"""Unambiguous JSON decoding for operator-supplied Lab registrations."""

from __future__ import annotations

import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate registration field")
        value[key] = item
    return value


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant: {token}")


def load_registration_json(raw: bytes) -> Any:
    """Decode JSON while refusing duplicate keys and non-standard constants."""

    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
