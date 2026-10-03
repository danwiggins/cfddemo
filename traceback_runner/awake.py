"""Keep the host awake while a local run holds a worker lease.

A worker lease is wall-clock time (``lease_expires_at`` in the runner store),
so it keeps running while the host sleeps but the worker and its renewal
threads do not.  On an unattended Mac the system idles to sleep within
minutes, and a sleep longer than the 30 s lease leaves the worker holding an
expired lease when it wakes: the fenced heartbeat then refuses it and the run
ends without a record (``TBX-JOB-001``).  Holding a power assertion for the
command's lifetime keeps the host awake through the run.

Best effort only: where the assertion tool is absent (non-macOS hosts) or
cannot start, the run proceeds and the lease fence stays the authority.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CAFFEINATE = Path("/usr/bin/caffeinate")


@contextmanager
def stay_awake() -> Iterator[subprocess.Popen[bytes] | None]:
    """Hold an idle- and system-sleep assertion until the block exits.

    ``-i`` prevents idle sleep and ``-s`` prevents system sleep on AC power
    (including the maintenance sleep that ends a dark wake).  ``-w`` ties the
    assertion to this process, so it is released even if this process is
    killed before the block exits.
    """

    holder: subprocess.Popen[bytes] | None = None
    if sys.platform == "darwin" and CAFFEINATE.is_file():
        try:
            holder = subprocess.Popen(
                [str(CAFFEINATE), "-i", "-s", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            holder = None
    try:
        yield holder
    finally:
        if holder is not None:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()
