"""Zero-hook capture for public aggregate-contract object boundaries."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import get_args, get_origin

from pydantic import BaseModel
from pydantic_core import TzInfo

DEFAULT_MAX_DEPTH = 128
DEFAULT_MAX_NODES = 2_000_000
DEFAULT_MAX_COLLECTION_ITEMS = 500_000
DEFAULT_MAX_STRING_BYTES = 1_024
DEFAULT_MAX_BINARY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_INT_BITS = 64
_EXACT_PATH_TYPE = type(Path("."))


def _field_limit(field: object, name: str) -> int | None:
    for item in object.__getattribute__(field, "metadata"):
        value = getattr(item, name, None)
        if type(value) is int:
            return value
    return None


def _collect_types(
    annotation: object, models: set[type[BaseModel]], enums: set[type[Enum]]
) -> None:
    if isinstance(annotation, Enum):
        enums.add(type(annotation))
        return
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            if annotation in models:
                return
            models.add(annotation)
            fields = vars(annotation).get("__pydantic_fields__", {})
            for field in fields.values():
                _collect_types(
                    object.__getattribute__(field, "annotation"), models, enums
                )
            return
        if issubclass(annotation, Enum):
            enums.add(annotation)
            return
    origin = get_origin(annotation)
    if origin is not None or isinstance(annotation, UnionType):
        for item in get_args(annotation):
            _collect_types(item, models, enums)


def contract_type_graph(
    *roots: type[BaseModel],
) -> tuple[frozenset[type[BaseModel]], frozenset[type[Enum]]]:
    """Derive exact installed model/enum identities from pinned root schemas."""

    models: set[type[BaseModel]] = set()
    enums: set[type[Enum]] = set()
    for root in roots:
        _collect_types(root, models, enums)
    return frozenset(models), frozenset(enums)


def graph_is_safe(
    root: object,
    *,
    model_types: frozenset[type[BaseModel]],
    enum_types: frozenset[type[Enum]],
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_collection_items: int = DEFAULT_MAX_COLLECTION_ITEMS,
    max_string_bytes: int = DEFAULT_MAX_STRING_BYTES,
    max_binary_bytes: int = DEFAULT_MAX_BINARY_BYTES,
    max_int_bits: int = DEFAULT_MAX_INT_BITS,
) -> bool:
    """Inspect exact object state without invoking caller-owned hooks."""

    # value, depth, schema max length inherited from the containing field
    stack: list[tuple[object, int, int | None]] = [(root, 0, None)]
    seen: set[int] = set()
    nodes = 0
    while stack:
        value, depth, schema_max = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        value_type = type(value)
        if value is None or value_type is bool:
            continue
        if value_type is int:
            if value.bit_length() > max_int_bits:
                return False
            continue
        if value_type is float:
            if not math.isfinite(value):
                return False
            continue
        if value_type is str:
            limit = min(max_string_bytes, schema_max or max_string_bytes)
            if len(value) > limit or len(value.encode("utf-8")) > max_string_bytes:
                return False
            continue
        if value_type is bytes:
            if len(value) > min(max_binary_bytes, schema_max or max_binary_bytes):
                return False
            continue
        if value_type is datetime:
            timezone = object.__getattribute__(value, "tzinfo")
            if timezone is UTC:
                continue
            if type(timezone) is TzInfo and timezone.utcoffset(None) == timedelta(0):
                continue
            return False
        if value_type in enum_types:
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if value_type in model_types:
            try:
                state = object.__getattribute__(value, "__dict__")
            except (AttributeError, TypeError):
                return False
            fields = vars(value_type).get("__pydantic_fields__")
            if type(state) is not dict or type(fields) is not dict:
                return False
            if any(type(key) is not str for key in state) or not set(fields) <= set(
                state
            ):
                return False
            for name, field in fields.items():
                maximum = _field_limit(field, "max_length")
                stack.append((state[name], depth + 1, maximum))
            continue
        if value_type in {tuple, list}:
            limit = min(max_collection_items, schema_max or max_collection_items)
            if len(value) > limit:
                return False
            stack.extend((item, depth + 1, None) for item in value)
            continue
        if value_type is dict:
            limit = min(max_collection_items, schema_max or max_collection_items)
            if len(value) > limit:
                return False
            stack.extend((item, depth + 1, None) for item in value)
            stack.extend((item, depth + 1, None) for item in value.values())
            continue
        return False
    return True


def exact_model_bytes(
    value: object,
    expected_type: type[BaseModel],
    *,
    model_types: frozenset[type[BaseModel]],
    enum_types: frozenset[type[Enum]],
    max_bytes: int,
) -> bytes:
    if type(value) is not expected_type or not graph_is_safe(
        value,
        model_types=model_types,
        enum_types=enum_types,
        max_binary_bytes=max_bytes,
    ):
        raise TypeError("contract object graph is invalid")
    payload = expected_type.__pydantic_serializer__.to_python(
        value,
        mode="json",
        exclude_none=False,
        warnings="error",
    )
    content = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(content) > max_bytes:
        raise ValueError("contract exceeds byte bound")
    return content


def capture_exact_model(
    value: object,
    expected_type: type[BaseModel],
    *,
    model_types: frozenset[type[BaseModel]],
    enum_types: frozenset[type[Enum]],
    max_bytes: int,
) -> BaseModel:
    content = exact_model_bytes(
        value,
        expected_type,
        model_types=model_types,
        enum_types=enum_types,
        max_bytes=max_bytes,
    )
    captured = expected_type.model_validate_json(content)
    if (
        exact_model_bytes(
            captured,
            expected_type,
            model_types=model_types,
            enum_types=enum_types,
            max_bytes=max_bytes,
        )
        != content
    ):
        raise ValueError("contract is not canonical")
    return captured


def exact_bytes(value: object, *, max_bytes: int) -> bytes:
    if type(value) is not bytes or len(value) > max_bytes:
        raise TypeError("binary input is invalid")
    return value


def safe_local_path(value: object, *, max_length: int = 4096) -> Path:
    if type(value) is str:
        if len(value) > max_length or "\x00" in value:
            raise TypeError("local path is invalid")
        return Path(value)
    if type(value) is _EXACT_PATH_TYPE:
        parts = object.__getattribute__(value, "_parts")
        if type(parts) is not list or any(type(item) is not str for item in parts):
            raise TypeError("local path is invalid")
        rendered = str(value)
        if len(rendered) > max_length or "\x00" in rendered:
            raise TypeError("local path is invalid")
        return value
    raise TypeError("local path type is invalid")
