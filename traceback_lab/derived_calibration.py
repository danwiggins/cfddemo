"""Resolve an eligible derived row-mask input to its calibrated parent."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .file_access import open_regular_no_follow

if TYPE_CHECKING:
    from .store import LabStore

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _manifest(path: Path) -> tuple[dict, int] | None:
    try:
        descriptor = open_regular_no_follow(path)
        try:
            size = os.fstat(descriptor).st_size
            if not 1 <= size <= 1_048_576:
                return None
            raw = os.read(descriptor, 1_048_577)
            if len(raw) != size or os.fstat(descriptor).st_size != size:
                return None
        finally:
            os.close(descriptor)
        data = json.loads(raw)
        return (data, size) if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def calibrated_input_id(
    store: LabStore, input_id: str, derived: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a verified parent id for split/downsample, self for a full input.

    A derived id is a digest over its parent, transform, parameters and fixed
    salt. A damaged or unsupported derived manifest never inherits an asset.
    """

    from .derived import DERIVED_SCHEMA, downsample, split

    if not isinstance(input_id, str) or not _SHA.fullmatch(input_id):
        return None
    path = store.inputs_dir / input_id / "manifest.json"
    if not path.exists() and not path.is_symlink():
        return input_id if derived is None else None
    parsed = _manifest(path)
    if parsed is None:
        return None
    data, size = parsed
    if data.get("schema") != DERIVED_SCHEMA:
        return input_id if derived is None else None
    if size > 8192:
        return None
    parent = data.get("parent_input_id")
    parameters = data.get("parameters")
    if (data.get("input_id") != input_id or not isinstance(parent, str)
            or not _SHA.fullmatch(parent) or not isinstance(parameters, dict)):
        return None
    try:
        if data.get("transform") == "downsample" and set(parameters) == {"fraction"}:
            fraction = parameters["fraction"]
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                return None
            spec = downsample(fraction)
        elif data.get("transform") == "split" and set(parameters) == {"half"}:
            spec = split(parameters["half"])
        else:
            return None
        if (data.get("salt") != spec.salt or spec.input_id(parent) != input_id
                or data.get("label") != spec.label()
                or (derived is not None and dict(derived) != spec.mask_spec())):
            return None
    except (TypeError, ValueError):
        return None
    return parent
