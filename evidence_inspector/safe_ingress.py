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
DEFAULT_MAX_EXPANDED_NODES = DEFAULT_MAX_NODES
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


def _closed_model_state(
    value: object, value_type: type[BaseModel]
) -> tuple[dict[str, object], dict[str, object]] | None:
    try:
        state = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
    except (AttributeError, TypeError):
        return None
    fields = vars(value_type).get("__pydantic_fields__")
    if type(state) is not dict or type(fields) is not dict:
        return None
    if any(type(key) is not str for key in state) or set(state) != set(fields):
        return None
    if extra is not None and (type(extra) is not dict or len(extra) != 0):
        return None
    if private is not None and (type(private) is not dict or len(private) != 0):
        return None
    return state, fields


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

    # value, depth, schema max length inherited from the containing field,
    # and whether this is the matching traversal-exit marker.
    stack: list[tuple[object, int, int | None, bool]] = [(root, 0, None, False)]
    active: set[int] = set()
    nodes = 0
    while stack:
        value, depth, schema_max, exiting = stack.pop()
        if exiting:
            active.remove(id(value))
            continue
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
        if identity in active:
            return False
        active.add(identity)
        stack.append((value, depth, schema_max, True))
        if value_type in model_types:
            closed_state = _closed_model_state(value, value_type)
            if closed_state is None:
                return False
            state, fields = closed_state
            for name, field in fields.items():
                maximum = _field_limit(field, "max_length")
                stack.append((state[name], depth + 1, maximum, False))
            continue
        if value_type in {tuple, list}:
            limit = min(max_collection_items, schema_max or max_collection_items)
            if len(value) > limit:
                return False
            stack.extend((item, depth + 1, None, False) for item in value)
            continue
        if value_type is dict:
            limit = min(max_collection_items, schema_max or max_collection_items)
            if len(value) > limit:
                return False
            stack.extend((item, depth + 1, None, False) for item in value)
            stack.extend((item, depth + 1, None, False) for item in value.values())
            continue
        return False
    return True


def expanded_json_size_is_safe(
    root: object,
    *,
    model_types: frozenset[type[BaseModel]],
    enum_types: frozenset[type[Enum]],
    max_bytes: int,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_EXPANDED_NODES,
) -> bool:
    """Bound expanded JSON cost while charging every DAG alias occurrence."""

    memo: dict[int, tuple[int, int]] = {}
    active: set[int] = set()
    exceeded = max_bytes + 1
    nodes = 0

    def string_bound(value: str) -> int:
        # A JSON string uses at most a six-byte escape for each code point.
        return 2 + 6 * len(value)

    def cost(value: object, depth: int) -> int:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            return exceeded
        if depth > max_depth:
            return exceeded
        value_type = type(value)
        if value is None:
            return 4
        if value_type is bool:
            return 5
        if value_type is int:
            digits = max(1, value.bit_length() * 30103 // 100000 + 1)
            return digits + int(value < 0)
        if value_type is float:
            return 32
        if value_type is str:
            return string_bound(value)
        if value_type is bytes:
            return 2 + 6 * len(value)
        if value_type is datetime:
            return 66
        if value_type in enum_types:
            return cost(object.__getattribute__(value, "_value_"), depth + 1)

        identity = id(value)
        cached = memo.get(identity)
        if cached is not None:
            cached_cost, cached_nodes = cached
            nodes += cached_nodes - 1
            if nodes > max_nodes:
                return exceeded
            return cached_cost
        if identity in active:
            return exceeded
        subtree_start = nodes
        active.add(identity)
        try:
            if value_type in model_types:
                closed_state = _closed_model_state(value, value_type)
                if closed_state is None:
                    return exceeded
                state, fields = closed_state
                total = 2
                for index, name in enumerate(fields):
                    total += (1 if index else 0) + string_bound(name) + 1
                    total += cost(state[name], depth + 1)
                    if total > max_bytes:
                        return exceeded
            elif value_type in {tuple, list}:
                total = 2
                for index, item in enumerate(value):
                    total += (1 if index else 0) + cost(item, depth + 1)
                    if total > max_bytes:
                        return exceeded
            elif value_type is dict:
                total = 2
                for index, (key, item) in enumerate(value.items()):
                    key_cost = cost(key, depth + 1)
                    total += (1 if index else 0) + key_cost + 2
                    total += cost(item, depth + 1)
                    if total > max_bytes:
                        return exceeded
            else:
                return exceeded
        finally:
            active.remove(identity)
        memo[identity] = (total, nodes - subtree_start + 1)
        return total

    try:
        return cost(root, 0) <= max_bytes
    except (AttributeError, TypeError, ValueError):
        return False


def exact_model_bytes(
    value: object,
    expected_type: type[BaseModel],
    *,
    model_types: frozenset[type[BaseModel]],
    enum_types: frozenset[type[Enum]],
    max_bytes: int,
    max_nodes: int = DEFAULT_MAX_EXPANDED_NODES,
) -> bytes:
    if (
        type(value) is not expected_type
        or not expanded_json_size_is_safe(
            value,
            model_types=model_types,
            enum_types=enum_types,
            max_bytes=max_bytes,
            max_nodes=max_nodes,
        )
        or not graph_is_safe(
            value,
            model_types=model_types,
            enum_types=enum_types,
            max_binary_bytes=max_bytes,
            max_nodes=max_nodes,
        )
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
        raw_parts = object.__getattribute__(value, "_parts")
        if type(raw_parts) is not list:
            raise TypeError("local path is invalid")
        captured_parts = tuple(raw_parts)
        if any(type(item) is not str for item in captured_parts):
            raise TypeError("local path is invalid")
        captured = Path(*captured_parts)
        rendered = str(captured)
        if len(rendered) > max_length or "\x00" in rendered:
            raise TypeError("local path is invalid")
        return captured
    raise TypeError("local path type is invalid")
