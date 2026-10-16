"""Dependency-light file access shared by Lab assets and cohort metadata."""

from __future__ import annotations

import os
from pathlib import Path


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
