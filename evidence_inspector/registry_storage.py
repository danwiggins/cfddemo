"""Shared crash-recovery storage helpers for the D03-pattern registries.

Every registry that follows the D03 storage pattern (a private ``0700`` root,
``0600`` owner-only files, an append-only hash-chained journal, and an optional
content-addressed ``objects`` directory) uses these helpers for three
behaviours, so the family behaves the same way:

* **Staged root creation and restore.**  A new root is populated in a hidden
  sibling directory ``.<name>.staging-<32 hex>`` and published with one
  ``rename(2)``.  The final name therefore either does not exist or holds a
  complete registry; an interrupted creation never leaves a half-built root
  that blocks a retry.  ``rename(2)`` keeps the directory's inode, so
  registries whose head fence is keyed by root inode see the same identity.
* **Temporary-name ownership (the D05 rule).**  A ``.tmp-<32 hex>`` name inside
  a registry's private directories is owned by the registry and is unlinked
  unconditionally under the exclusive lock.  ``unlink(2)`` never follows a
  symlink and never destroys data with another link; a directory under that
  name makes ``unlink`` fail, so recovery fails closed.
* **Operator torn-tail recovery.**  Reopening a journal whose last line is
  incomplete fails closed.  ``recover_torn_journal_tail`` is an explicit
  maintenance entry point that removes only an unterminated trailing line,
  under the exclusive registry lock, and only when every complete line still
  chains from the metadata genesis to the operator's retained head.

Threat model: in-process code mutation and same-user filesystem races are out
of scope.  The helpers raise ``OSError`` (or the caller-supplied error type
for validation failures); each registry maps them to its own sanitized errors.
"""

from __future__ import annotations

import errno
import fcntl
import os
import secrets
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

TEMPORARY_PREFIX = ".tmp-"
STAGING_INFIX = ".staging-"
_HEX = frozenset("0123456789abcdef")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in _HEX for character in value)


def is_owned_temporary_name(name: object) -> bool:
    """Return whether ``name`` is a registry-owned ``.tmp-<32 hex>`` name."""

    return (
        type(name) is str
        and name.startswith(TEMPORARY_PREFIX)
        and _is_hex(name[len(TEMPORARY_PREFIX) :], 32)
    )


def remove_owned_temporaries(directory_fd: int) -> None:
    """Unlink every owned temporary name in one private directory (D05 rule).

    The caller holds the registry's exclusive lock.  Any failure, including a
    directory under an owned name, raises ``OSError`` so the caller fails
    closed.
    """

    for name in os.listdir(directory_fd):
        if is_owned_temporary_name(name):
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def staging_name(final_name: str) -> str:
    """Return a fresh hidden sibling name for staging ``final_name``."""

    return f".{final_name}{STAGING_INFIX}{secrets.token_hex(16)}"


def _require_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FileExistsError(errno.EEXIST, "registry target already exists")


def make_staging_directory(parent_fd: int, final_name: str) -> str:
    """Create a private staging directory beside ``final_name`` and return its name.

    Raises ``FileExistsError`` when ``final_name`` already exists, so a
    restore or creation never stages over an existing target.
    """

    _require_absent(parent_fd, final_name)
    name = staging_name(final_name)
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    return name


def commit_staging_directory(
    parent_fd: int, staged_name: str, final_name: str, root_fd: int
) -> None:
    """Publish a fully populated staging directory under its final name.

    The staged directory must still be the one ``root_fd`` holds.  The final
    name must not exist; ``rename(2)`` keeps the inode, and the parent is
    fsynced so the publication is durable.
    """

    os.fsync(root_fd)
    bound = os.fstat(root_fd)
    observed = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
        bound.st_dev,
        bound.st_ino,
    ):
        raise OSError(errno.ESTALE, "registry staging directory changed")
    _require_absent(parent_fd, final_name)
    try:
        os.rename(staged_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(
                errno.EEXIST, "registry target already exists"
            ) from None
        raise
    os.fsync(parent_fd)
    landed = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
    if (landed.st_dev, landed.st_ino) != (bound.st_dev, bound.st_ino):
        raise OSError(errno.ESTALE, "registry staging publication changed")


def bound_name(parent_fd: int, root_fd: int, staged_name: str, final_name: str) -> str:
    """Return whichever of the two names holds the directory ``root_fd`` holds.

    A failure or interrupt can land after ``rename(2)`` but before the caller
    learns of it; cleanup must then address the published name, not the
    vanished staging name.
    """

    try:
        bound = os.fstat(root_fd)
        observed = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return staged_name
    if (observed.st_dev, observed.st_ino) == (bound.st_dev, bound.st_ino):
        return final_name
    return staged_name


def remove_staging_directory(parent_fd: int, name: str) -> None:
    """Best-effort removal of one abandoned staging tree (two levels deep)."""

    try:
        root_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError:
        return
    try:
        for entry in os.listdir(root_fd):
            try:
                os.unlink(entry, dir_fd=root_fd)
            except OSError:
                try:
                    child_fd = os.open(entry, _DIRECTORY_FLAGS, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    for child in os.listdir(child_fd):
                        os.unlink(child, dir_fd=child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(entry, dir_fd=root_fd)
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass
    finally:
        os.close(root_fd)


def _open_parent(path: Path) -> int:
    # Resolve symlinked ancestors (for example macOS /tmp) first, as the
    # pre-staging mkdir did; the inode check then binds the real directory.
    path = Path(os.path.realpath(path))
    parent_lstat = os.stat(path, follow_symlinks=False)
    parent_fd = os.open(path, _DIRECTORY_FLAGS)
    bound = os.fstat(parent_fd)
    if not stat.S_ISDIR(parent_lstat.st_mode) or (
        parent_lstat.st_dev,
        parent_lstat.st_ino,
    ) != (bound.st_dev, bound.st_ino):
        os.close(parent_fd)
        raise OSError(errno.ESTALE, "registry parent changed")
    return parent_fd


def begin_staged_root(final: Path) -> Path | None:
    """Return a new private staging root beside ``final``, or ``None`` if it exists.

    An existing final path (including a symlink or an incomplete legacy root)
    is never staged over; the caller opens it through its normal checks.
    """

    try:
        os.stat(final, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        return None
    final.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_parent(final.parent)
    try:
        name = make_staging_directory(parent_fd, final.name)
    finally:
        os.close(parent_fd)
    return final.parent / name


def commit_staged_root(staged: Path, final: Path, root_fd: int) -> None:
    """Publish a staged root created by ``begin_staged_root`` under ``final``."""

    parent_fd = _open_parent(final.parent)
    try:
        commit_staging_directory(parent_fd, staged.name, final.name, root_fd)
    finally:
        os.close(parent_fd)


def discard_staged_root(staged: Path, final: Path, root_fd: int | None) -> None:
    """Best-effort removal of a staged root whose creation did not complete.

    If the failure landed after ``rename(2)``, the directory ``root_fd``
    holds is now at ``final``; it is a brand-new registry whose identity was
    never returned, so it is removed under that name instead.
    """

    try:
        parent_fd = _open_parent(staged.parent)
    except OSError:
        return
    try:
        name = staged.name
        if root_fd is not None:
            name = bound_name(parent_fd, root_fd, staged.name, final.name)
        remove_staging_directory(parent_fd, name)
    finally:
        os.close(parent_fd)


class _ChainEntry(Protocol):
    sequence: int
    previous_entry_sha256: str
    entry_sha256: str


class _Metadata(Protocol):
    registry_id: str
    registry_epoch_sha256: str


def _open_private(name: str, flags: int, directory_fd: int, *, directory: bool) -> int:
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    observed = os.fstat(descriptor)
    kind_ok = (
        stat.S_ISDIR(observed.st_mode) if directory else stat.S_ISREG(observed.st_mode)
    )
    if (
        not kind_ok
        or stat.S_IMODE(observed.st_mode) != (0o700 if directory else 0o600)
        or observed.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise OSError(errno.EPERM, "registry storage is not private")
    return descriptor


def _pread_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.pread(descriptor, min(64 * 1024, maximum + 1 - total), total)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise OSError(errno.EFBIG, "registry file exceeds its bound")
        chunks.append(chunk)


def recover_torn_journal_tail(
    root: Path,
    *,
    expected_registry_id: object,
    expected_registry_epoch_sha256: object,
    expected_state_head_sha256: object,
    parse_metadata: Callable[[bytes], _Metadata],
    genesis_sha256: Callable[[_Metadata], str],
    parse_entry: Callable[[bytes], _ChainEntry],
    entry_sha256: Callable[[_ChainEntry], str],
    max_journal_bytes: int,
    max_entries: int,
    process_lock: threading.RLock,
    error: type[Exception],
    label: str,
) -> int:
    """Remove an unterminated trailing journal line; return the bytes removed.

    Operator-invoked only; nothing calls it on reopen.  It takes the registry's
    exclusive lock without waiting: another thread holding the process lock,
    or any live flock (including the caller's own fence, which re-enters the
    process RLock), is refused as "in use".  It then checks the private root,
    metadata, and journal; requires
    the metadata identity to equal the retained identity; and requires every
    complete line to chain from the metadata genesis to exactly the retained
    head.  Only then does it truncate the bytes after the last newline.  A
    journal that already ends with a newline is left untouched (``0``).
    Committed entries are newline-terminated and fsynced before any receipt,
    so this never removes a committed entry.
    """

    if (
        type(expected_registry_id) is not str
        or type(expected_registry_epoch_sha256) is not str
        or type(expected_state_head_sha256) is not str
        or not _is_hex(expected_registry_epoch_sha256, 64)
        or not _is_hex(expected_state_head_sha256, 64)
    ):
        raise error(f"{label} expected identity or head is invalid")
    root_fd: int | None = None
    lock_fd: int | None = None
    journal_fd: int | None = None
    try:
        root_lstat = os.stat(root, follow_symlinks=False)
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        bound = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_lstat.st_mode)
            or (root_lstat.st_dev, root_lstat.st_ino) != (bound.st_dev, bound.st_ino)
            or stat.S_IMODE(bound.st_mode) != 0o700
            or bound.st_uid != os.geteuid()
        ):
            raise error(f"{label} root must be private")
        lock_fd = _open_private(
            ".registry.lock", os.O_RDWR | _FILE_FLAGS, root_fd, directory=False
        )
        # Never wait: maintenance on a registry that a live instance holds is
        # refused rather than queued, so it cannot deadlock a fence.  Another
        # thread holding the process lock is refused here; the process lock is
        # an RLock, so the caller's own fence re-enters it and is refused by
        # the non-blocking flock below instead.
        if not process_lock.acquire(blocking=False):
            raise error(f"{label} is in use")
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise error(f"{label} is in use") from None
            try:
                metadata_fd = _open_private(
                    "registry-metadata.json",
                    os.O_RDONLY | _FILE_FLAGS,
                    root_fd,
                    directory=False,
                )
                try:
                    metadata_bytes = _pread_bounded(metadata_fd, 4096)
                finally:
                    os.close(metadata_fd)
                try:
                    metadata = parse_metadata(metadata_bytes)
                    genesis = genesis_sha256(metadata)
                except Exception:
                    raise error(f"{label} metadata is invalid") from None
                if (metadata.registry_id, metadata.registry_epoch_sha256) != (
                    expected_registry_id,
                    expected_registry_epoch_sha256,
                ):
                    raise error(f"{label} expected identity or head is invalid")
                journal_fd = _open_private(
                    "registry-journal.jsonl",
                    os.O_RDWR | _FILE_FLAGS,
                    root_fd,
                    directory=False,
                )
                content = _pread_bounded(journal_fd, max_journal_bytes)
                committed = content.rfind(b"\n") + 1
                lines = content[:committed].splitlines()
                if len(lines) > max_entries:
                    raise error(f"{label} journal bound exceeded")
                previous = genesis
                for sequence, line in enumerate(lines, start=1):
                    try:
                        entry = parse_entry(line)
                        valid = (
                            entry.sequence == sequence
                            and entry.previous_entry_sha256 == previous
                            and entry.entry_sha256 == entry_sha256(entry)
                        )
                    except Exception:
                        valid = False
                    if not valid:
                        raise error(f"{label} journal is invalid")
                    previous = entry.entry_sha256
                if previous != expected_state_head_sha256:
                    raise error(f"{label} expected identity or head is invalid")
                removed = len(content) - committed
                if removed:
                    os.ftruncate(journal_fd, committed)
                    os.fsync(journal_fd)
                return removed
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            process_lock.release()
    except OSError:
        raise error(f"{label} torn-tail recovery failed") from None
    finally:
        for descriptor in (journal_fd, lock_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


__all__ = [
    "STAGING_INFIX",
    "TEMPORARY_PREFIX",
    "begin_staged_root",
    "bound_name",
    "commit_staged_root",
    "commit_staging_directory",
    "discard_staged_root",
    "is_owned_temporary_name",
    "make_staging_directory",
    "recover_torn_journal_tail",
    "remove_owned_temporaries",
    "remove_staging_directory",
    "staging_name",
]
