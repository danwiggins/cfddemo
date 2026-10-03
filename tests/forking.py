"""Fork helpers that never let a child process return into the pytest session.

A forked child that raises would otherwise unwind into pytest, run the rest of
the session a second time and, under xdist, corrupt the worker channel. Every
path out of a child here ends in ``os._exit``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

CHILD_RAISED = 70


def start_child(body: Callable[[], object]) -> int:
    """Fork a child that runs ``body`` and always ends in ``os._exit``.

    ``body`` returning (any value) exits 0; an exception exits ``CHILD_RAISED``
    after writing its type to stderr. A child that exits itself (a fault
    controller's ``os._exit``) keeps its own code. Returns the child's pid.
    """

    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        code = CHILD_RAISED
        try:
            body()
            code = 0
        except BaseException as exc:  # noqa: BLE001 - child reports only exit status
            try:
                sys.stderr.write(f"forked child raised {type(exc).__name__}: {exc}\n")
            except BaseException:  # noqa: BLE001
                pass
        finally:
            os._exit(code)
    return pid


def wait_child(pid: int) -> int:
    """Reap ``pid``; a signal death is returned as a negative signal number."""

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def run_in_child(body: Callable[[], object]) -> int:
    """Run ``body`` in a forked child and return the child's exit code."""

    return wait_child(start_child(body))
