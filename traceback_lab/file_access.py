"""Dependency-light file access shared by Lab assets and cohort metadata."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def open_regular_no_follow(path: Path) -> int:
    """Open a regular-file candidate without following symlink components on POSIX."""

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


def open_directory_no_follow(path: Path) -> int:
    """Open a directory with every ancestor fixed to a nonsymlink dirfd."""

    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        result = directory
        directory = -1
        return result
    finally:
        if directory >= 0:
            os.close(directory)


@contextmanager
def directory_no_follow(path: Path) -> Iterator[int]:
    descriptor = open_directory_no_follow(path)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def write_atomic_no_follow(path: Path, content: bytes) -> None:
    """Replace a file using a pinned, nonsymlink parent chain and sync it."""

    with directory_no_follow(path.parent) as directory:
        temporary = f".{path.name}.{os.urandom(8).hex()}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(temporary, flags, 0o600, dir_fd=directory), "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
