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


_LOCK_TYPE = type(threading.Lock())
_EVENT_TYPE = threading.Event
_LOCK_ENTER = _LOCK_TYPE.__enter__
_LOCK_EXIT = _LOCK_TYPE.__exit__
_EVENT_SET = _EVENT_TYPE.set
_EVENT_WAIT = _EVENT_TYPE.wait


def validate_fault_controller(
    controller: object,
) -> tuple[str | None, FaultAction, int]:
    """Capture exact controller primitives without dispatching to caller code."""

    if type(controller) is not DeterministicFaultController:
        raise TypeError("fault controller must be exact")
    point = object.__getattribute__(controller, "_point")
    action = object.__getattribute__(controller, "_action")
    exit_code = object.__getattribute__(controller, "_exit_code")
    armed = object.__getattribute__(controller, "_armed")
    fired = object.__getattribute__(controller, "_fired")
    lock = object.__getattribute__(controller, "_lock")
    reached = object.__getattribute__(controller, "_reached")
    released = object.__getattribute__(controller, "_released")
    sealed = object.__getattribute__(controller, "_sealed")
    if point is not None and type(point) is not str:
        raise TypeError("fault point must be an exact string")
    if point is not None and point not in FAULT_POINTS:
        raise ValueError("fault point is invalid")
    if type(action) is not FaultAction:
        raise TypeError("fault action must be exact")
    if type(exit_code) is not int or not 1 <= exit_code <= 255:
        raise ValueError("fault exit code is invalid")
    if type(armed) is not bool or type(fired) is not bool or type(sealed) is not bool:
        raise TypeError("fault controller state is invalid")
    if armed is not (point is not None) or not sealed:
        raise TypeError("fault controller state is invalid")
    if type(lock) is not _LOCK_TYPE:
        raise TypeError("fault controller lock is invalid")
    if type(reached) is not _EVENT_TYPE or type(released) is not _EVENT_TYPE:
        raise TypeError("fault controller event is invalid")
    return point, action, exit_code


def fault_controller_snapshot(
    controller: object,
) -> tuple[str | None, FaultAction, int, int, int, int]:
    """Bind configuration and every retained synchronization primitive."""

    point, action, exit_code = validate_fault_controller(controller)
    return (
        point,
        action,
        exit_code,
        id(object.__getattribute__(controller, "_lock")),
        id(object.__getattribute__(controller, "_reached")),
        id(object.__getattribute__(controller, "_released")),
    )


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
        return validate_fault_controller(self)

    @property
    def fired(self) -> bool:
        return self._fired

    def hit(self, point: str) -> None:
        if type(point) is not str:
            raise TypeError("fault point must be an exact string")
        if point not in FAULT_POINTS:
            raise ValueError("fault point is invalid")
        configured_point, action, exit_code = validate_fault_controller(self)
        lock = object.__getattribute__(self, "_lock")
        reached = object.__getattribute__(self, "_reached")
        released = object.__getattribute__(self, "_released")
        _LOCK_ENTER(lock)
        try:
            # Revalidate after acquiring the retained exact lock. Concurrent
            # replacement can only fail closed before any replacement is used.
            if validate_fault_controller(self) != (
                configured_point,
                action,
                exit_code,
            ):
                raise TypeError("fault controller changed")
            if (
                object.__getattribute__(self, "_lock") is not lock
                or object.__getattribute__(self, "_reached") is not reached
                or object.__getattribute__(self, "_released") is not released
            ):
                raise TypeError("fault controller changed")
            if (
                not object.__getattribute__(self, "_armed")
                or object.__getattribute__(self, "_fired")
                or point != configured_point
            ):
                return
            object.__setattr__(self, "_fired", True)
            _EVENT_SET(reached)
        finally:
            _LOCK_EXIT(lock, None, None, None)
        if action is FaultAction.EXIT:
            os._exit(exit_code)
        if action is FaultAction.RAISE:
            raise InjectedFault(f"injected fault at {point}")
        if not _EVENT_WAIT(released, timeout=30):
            raise InjectedFault(f"fault pause timed out at {point}")
        if action is FaultAction.PAUSE_RAISE:
            raise InjectedFault(f"injected fault at {point}")

    def wait_until_reached(self, timeout: float = 10) -> bool:
        validate_fault_controller(self)
        reached = object.__getattribute__(self, "_reached")
        return _EVENT_WAIT(reached, timeout=timeout)

    def release(self) -> None:
        validate_fault_controller(self)
        released = object.__getattribute__(self, "_released")
        _EVENT_SET(released)


NO_FAULTS = DeterministicFaultController()


__all__ = [
    "FAULT_POINTS",
    "NO_FAULTS",
    "DeterministicFaultController",
    "FaultAction",
    "InjectedFault",
    "fault_controller_snapshot",
    "validate_fault_controller",
]
