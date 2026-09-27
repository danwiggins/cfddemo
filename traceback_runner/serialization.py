"""Canonical serialization primitives for signed runner contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

ContractT = TypeVar("ContractT", bound=BaseModel)


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite number at {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC-8259 JSON with stable keys, separators, and UTF-8 bytes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    _reject_nonfinite(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_model_from_bytes(model: type[ContractT], content: bytes) -> ContractT:
    """Validate canonical bytes and return the exact strict model they encode."""

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON token: {token}")

    try:
        value = json.loads(content, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("content is not valid UTF-8 JSON") from exc
    parsed = model.model_validate(value)
    if canonical_json_bytes(parsed) != content:
        raise ValueError("content is valid but not canonical JSON")
    return parsed


def sha256_bytes(content: bytes) -> str:
    """Return a lowercase SHA-256 digest of exact bytes."""

    return hashlib.sha256(content).hexdigest()
