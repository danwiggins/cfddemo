"""Strict, immutable contract primitives shared by the evidence inspector.

The models in this module contain no file access, provider calls, or scientific
computation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    """Base for public contracts: immutable and closed to unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data deterministically for content hashing."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    return hashlib.sha256(content).hexdigest()
