"""Package-owned deterministic fault points for offline recovery tests.

Production uses the exact no-op controller. Tests may request a single raise,
process exit, or synchronization pause without executing caller code inside a
catalog transaction.
"""

from __future__ import annotations

import os
import threading
from enum import StrEnum


FAULT_POINTS = frozenset(
    {
        "after_bundle_snapshot",
        "after_object_publish",
        "before_catalog_commit",
        "after_preflight",
        "before_idempotent_return",
        "after_result_stage",
        "before_binding_publish",
        "after_binding_publish",
        "before_visibility",
        "after_visibility_staged",
        "after_visibility_commit",
        "before_read_return",
        "before_status_return",
    }
)


class FaultAction(StrEnum):
    RAISE = "raise"
    EXIT = "exit"
    PAUSE = "pause"
    PAUSE_RAISE = "pause_raise"


class InjectedFault(RuntimeError):
    """Deterministic package-owned test interruption."""


class DeterministicFaultController:
    __slots__ = (
        "_action",
        "_armed",
        "_exit_code",
        "_fired",
        "_lock",
        "_point",
        "_reached",
        "_released",
        "_sealed",
    )

    def __init_subclass__(cls, **kwargs) -> None:
        del kwargs
        raise TypeError("fault controller cannot be subclassed")

    def __init__(
        self,
        point: str | None = None,
        *,
        action: FaultAction = FaultAction.RAISE,
        exit_code: int = 73,
    ) -> None:
        # Reject caller objects before any operation that could dispatch to caller
        # code. Fault points are package literals, not an extension interface.
        if point is not None and type(point) is not str:
            raise TypeError("fault point must be an exact string")
        if point is not None and point not in FAULT_POINTS:
            raise ValueError("fault point is invalid")
        if type(action) is not FaultAction:
            raise TypeError("fault action must be exact")
        if type(exit_code) is not int or not 1 <= exit_code <= 255:
            raise ValueError("fault exit code is invalid")
        object.__setattr__(self, "_sealed", False)
        object.__setattr__(self, "_point", point)
        object.__setattr__(self, "_action", action)
        object.__setattr__(self, "_exit_code", exit_code)
        object.__setattr__(self, "_armed", point is not None)
        object.__setattr__(self, "_fired", False)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_reached", threading.Event())
        object.__setattr__(self, "_released", threading.Event())
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        if getattr(self, "_sealed", False):
            raise TypeError("fault controller is immutable")
        raise TypeError("fault controller state is package-owned")

    @property
    def configuration(self) -> tuple[str | None, FaultAction, int]:
        return self._point, self._action, self._exit_code

    @property
    def fired(self) -> bool:
        return self._fired

    def hit(self, point: str) -> None:
        if type(self) is not DeterministicFaultController:
            raise TypeError("fault controller must be exact")
        if type(point) is not str:
            raise TypeError("fault point must be an exact string")
        if point not in FAULT_POINTS:
            raise ValueError("fault point is invalid")
        with self._lock:
            if not self._armed or self._fired or point != self._point:
                return
            object.__setattr__(self, "_fired", True)
            self._reached.set()
        if self._action is FaultAction.EXIT:
            os._exit(self._exit_code)
        if self._action is FaultAction.RAISE:
            raise InjectedFault(f"injected fault at {point}")
        if not self._released.wait(timeout=30):
            raise InjectedFault(f"fault pause timed out at {point}")
        if self._action is FaultAction.PAUSE_RAISE:
            raise InjectedFault(f"injected fault at {point}")

    def wait_until_reached(self, timeout: float = 10) -> bool:
        return self._reached.wait(timeout=timeout)

    def release(self) -> None:
        self._released.set()


NO_FAULTS = DeterministicFaultController()


__all__ = [
    "FAULT_POINTS",
    "NO_FAULTS",
    "DeterministicFaultController",
    "FaultAction",
    "InjectedFault",
]
