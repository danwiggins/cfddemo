"""Small fail-closed filesystem primitives shared by local publishers."""

from __future__ import annotations

import ctypes
import errno
import os
import sys


def rename_directory_exclusive_at(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Atomically rename one child directory without replacing a destination."""

    if (
        "/" in source_name
        or "/" in destination_name
        or source_name in {"", ".", ".."}
    ):
        raise ValueError("exclusive rename requires simple child names")
    if destination_name in {".", ".."}:
        raise ValueError("exclusive rename requires simple child names")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(destination_name),
            0x4,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(destination_name),
            1,  # RENAME_NOREPLACE
        )
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory publication is unsupported")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination_name)
        raise OSError(error, os.strerror(error), destination_name)


__all__ = ["rename_directory_exclusive_at"]
