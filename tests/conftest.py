"""Suite-wide isolation: no test inherits the operator's ``TRACEBACK_ROOT``.

Both surfaces resolve ROOT from ``$TRACEBACK_ROOT`` when ``--root`` is not
given, so an exported value would redirect a test's writes into the
operator's real store.  Tests that exercise the variable set it themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_traceback_root():
    # A private MonkeyPatch: requesting the shared ``monkeypatch`` fixture here
    # would set it up before every test's own fixtures and undo it after them,
    # so a registry fixture would close while a test's class patch was live.
    patch = pytest.MonkeyPatch()
    patch.delenv("TRACEBACK_ROOT", raising=False)
    try:
        yield
    finally:
        patch.undo()
