"""Operator-registered, exact-byte tumour/healthy methylation marker reference.

No marker panel is bundled or downloaded. Registration records provenance and
terms; execution rehashes the immutable local bytes and binds their digest to
the result identity. The fractions are operator-supplied reference values, not
clinical calibration or a validated tumour classifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from traceback_runner.serialization import canonical_json_bytes

from .asset_contracts import AssetDescriptor
from .assets import (
    AssetError,
    _check_contained,
    _file_identity,
    _open_regular,
    _regular_source,
)
from .identity import SHA256_PATTERN

if TYPE_CHECKING:
    from .store import LabStore

KIND = "meth-tf-reference"
FILENAME = "reference.json"
SCHEMA = "traceback.lab.meth-tf-reference.v1"
MANIFEST_SCHEMA = "traceback.lab.asset-registration.v1"
MAX_BYTES = 32 << 20
_MODEL = re.compile(r"^[A-Za-z0-9._@+-]{1,160}$")
_CHROM = re.compile(r"^chr(?:[1-9]|1[0-9]|2[0-2])$")


class Marker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chrom: str
    pos0: int = Field(ge=0)
    healthy: float = Field(ge=0, le=1, allow_inf_nan=False)
    tumour: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _chromosome(self) -> Marker:
        if not _CHROM.fullmatch(self.chrom):
            raise ValueError("markers must use autosomal chr1–chr22 coordinates")
        return self


class MethTFReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["traceback.lab.meth-tf-reference.v1"] = Field(alias="schema")
    reference_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    modbase_model: str
    markers: tuple[Marker, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _markers(self) -> MethTFReference:
        if not _MODEL.fullmatch(self.modbase_model):
            raise ValueError("reference schema or modified-base model is invalid")
        keys = tuple((marker.chrom, marker.pos0) for marker in self.markers)
        if keys != tuple(sorted(set(keys), key=lambda item: (int(item[0][3:]), item[1]))):
            raise ValueError("markers must be unique and sorted in chromosome/position order")
        return self


@dataclass(frozen=True)
class RegisteredReference:
    descriptor: AssetDescriptor
    reference: MethTFReference
    file: Path

    @property
    def sha256(self) -> str:
        return self.descriptor.sha256

    @property
    def asset_id(self) -> str:
        return self.descriptor.asset_id

    @property
    def legacy(self) -> bool:
        return False

    def public(self) -> dict[str, object]:
        return {
            **self.descriptor.model_dump(by_alias=True), "asset_id": self.descriptor.asset_id,
            "file": str(self.file), "markers": len(self.reference.markers),
            "reference_sha256": self.reference.reference_sha256,
            "modbase_model": self.reference.modbase_model,
        }


def _read_regular(path: Path, *, limit: int) -> bytes:
    """Read one no-follow handle, refusing file or pathname mutation."""
    try:
        with os.fdopen(_open_regular(path), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetError("asset must be a regular file")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = os.lstat(path)
    except OSError as exc:
        raise AssetError("asset is absent, unreadable, or changed during read") from exc
    if (len(raw) > limit or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(current)):
        raise AssetError("asset is too large or changed during read")
    return raw


def _parse_reference(raw: bytes) -> MethTFReference:
    try:
        return MethTFReference.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise AssetError("meth-TF reference is not a valid marker document") from exc


def _paths(store: LabStore) -> tuple[Path, Path, Path]:
    directory = store.lab / "assets" / KIND
    file = directory / FILENAME
    manifest = directory / "manifest.json"
    for path in (directory, file, manifest):
        _check_contained(store, path)
    return directory, file, manifest


def inspect_reference(store: LabStore, *, expected_sha256: str | None = None) -> RegisteredReference:
    """Validate one registered asset's metadata and bytes before use."""
    _, file, manifest_path = _paths(store)
    raw = _read_regular(file, limit=MAX_BYTES)
    manifest_raw = _read_regular(manifest_path, limit=65_536)
    try:
        manifest = json.loads(manifest_raw)
        if (set(manifest) != {"schema", "descriptor", "file", "registered_utc"}
                or manifest["schema"] != MANIFEST_SCHEMA or manifest["file"] != FILENAME):
            raise ValueError("unexpected manifest")
        descriptor = AssetDescriptor.model_validate(manifest["descriptor"])
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        raise AssetError("meth-TF asset manifest is invalid") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if (descriptor.kind != KIND or descriptor.origin != "user" or descriptor.sha256 != digest
            or descriptor.size_bytes != len(raw) or (expected_sha256 is not None and digest != expected_sha256)):
        raise AssetError("meth-TF reference differs from its registered SHA-256")
    return RegisteredReference(descriptor, _parse_reference(raw), file)


def add_reference(
    store: LabStore, source: Path, *, source_label: str, terms: str,
    license_id: str | None = None,
) -> RegisteredReference:
    """Publish exact validated bytes under the held Lab writer lock."""
    source = _regular_source(source)
    raw = _read_regular(source, limit=MAX_BYTES)
    _parse_reference(raw)
    if (source_label != source_label.strip() or not source_label
            or any(char in source_label for char in ("/", "\\"))
            or any(ord(char) < 32 for char in source_label)):
        raise AssetError("source label must be a short description, not a path")
    try:
        descriptor = AssetDescriptor(
            kind=KIND, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
            origin="user", source_label=source_label, terms=terms, license_id=license_id,
        )
    except ValidationError as exc:
        raise AssetError("invalid asset descriptor: source, terms, or license") from exc
    directory, _, _ = _paths(store)
    if directory.exists() or directory.is_symlink():
        registered = inspect_reference(store)
        if registered.descriptor != descriptor:
            raise AssetError("a different meth-TF reference is already registered in this Lab root")
        return registered
    manifest = {
        "schema": MANIFEST_SCHEMA, "descriptor": descriptor.model_dump(by_alias=True),
        "file": FILENAME, "registered_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    def build(staging: Path) -> None:
        (staging / FILENAME).write_bytes(raw)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    store.publish_directory(directory.parent, KIND, build)
    return inspect_reference(store, expected_sha256=descriptor.sha256)


__all__ = ["KIND", "MethTFReference", "RegisteredReference", "add_reference", "inspect_reference"]
