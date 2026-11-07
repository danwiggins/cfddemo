"""Suite-wide isolation: no test inherits the operator's ``TRACEBACK_ROOT``.

Both surfaces resolve ROOT from ``$TRACEBACK_ROOT`` when ``--root`` is not
given, so an exported value would redirect a test's writes into the
operator's real store.  Tests that exercise the variable set it themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_traceback_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEBACK_ROOT", raising=False)
