"""Operator-registered Somalier hg38 sites, pinned by exact SHA-256 bytes."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained, _regular_source
from .file_access import open_regular_no_follow
from .store import LabStore

SITES_SCHEMA = "traceback.lab.identity-sites.v1"
SITES_NAME = "sites.hg38.vcf.gz"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_UNCOMPRESSED_BYTES = 512 << 20


class IdentitySitesError(ValueError):
    """The registered sites cannot safely be used."""


@dataclass(frozen=True)
class IdentitySites:
    file: Path
    sha256: str
    size_bytes: int
    source_label: str


def _directory(store: LabStore) -> Path:
    return store.lab / "assets" / "identity-sites"


def _hash_regular(path: Path) -> tuple[str, int]:
    try:
        descriptor = open_regular_no_follow(path)
    except OSError as exc:
        raise IdentitySitesError("identity sites are absent or unsafe") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IdentitySitesError("identity sites must be one regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while block := stream.read(1 << 20):
                size += len(block)
                digest.update(block)
        after = os.fstat(descriptor)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise IdentitySitesError("identity sites changed while being read")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def inspect_sites(store: LabStore) -> IdentitySites:
    directory = _directory(store)
    manifest = directory / "manifest.json"
    file = directory / SITES_NAME
    try:
        _check_contained(store, manifest)
        _check_contained(store, file)
        descriptor = open_regular_no_follow(manifest)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("manifest too large")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise IdentitySitesError("identity sites registration is unreadable") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "sha256", "size_bytes", "source_label"}
            or data["schema"] != SITES_SCHEMA or not isinstance(data["sha256"], str)
            or not SHA256.fullmatch(data["sha256"]) or not isinstance(data["size_bytes"], int)
            or isinstance(data["size_bytes"], bool) or data["size_bytes"] <= 0
            or not isinstance(data["source_label"], str) or not 1 <= len(data["source_label"]) <= 160):
        raise IdentitySitesError("identity sites registration is invalid")
    digest, size = _hash_regular(file)
    if digest != data["sha256"] or size != data["size_bytes"]:
        raise IdentitySitesError("identity sites differ from the registered bytes")
    return IdentitySites(file, digest, size, data["source_label"])


def register_sites(store: LabStore, source: Path, expected_sha256: str, source_label: str) -> IdentitySites:
    """Copy a local VCF under the held writer lock; never download donor assets."""

    if not SHA256.fullmatch(expected_sha256) or not 1 <= len(source_label) <= 160:
        raise IdentitySitesError("provide a SHA-256 and a short source label")
    try:
        source = _regular_source(source)
    except Exception as exc:  # noqa: BLE001 - source errors are a blocked asset
        raise IdentitySitesError("identity sites source is absent or unsafe") from exc
    digest, size = _hash_regular(source)
    if digest != expected_sha256:
        raise IdentitySitesError("identity sites source does not match the expected SHA-256")
    try:
        with gzip.open(source, "rb") as stream:
            header = stream.readline(256)
            expanded = len(header)
            while block := stream.read(1 << 20):
                expanded += len(block)
                if expanded > MAX_UNCOMPRESSED_BYTES:
                    raise IdentitySitesError("identity sites exceed the expanded size limit")
    except (OSError, EOFError, zlib.error) as exc:
        raise IdentitySitesError("identity sites are not a readable gzip VCF") from exc
    if not header.startswith(b"##fileformat=VCF"):
        raise IdentitySitesError("identity sites are not a VCF")
    directory = _directory(store)
    try:
        _check_contained(store, directory.parent)
    except AssetError as exc:
        raise IdentitySitesError("identity sites destination is unsafe") from exc
    if directory.exists() or directory.is_symlink():
        existing = inspect_sites(store)
        if existing.sha256 != digest or existing.source_label != source_label:
            raise IdentitySitesError("different identity sites are already registered")
        return existing

    def build(staging: Path) -> None:
        target = staging / SITES_NAME
        with os.fdopen(open_regular_no_follow(source), "rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1 << 20)
        copied, copied_size = _hash_regular(target)
        if copied != digest or copied_size != size:
            raise IdentitySitesError("identity sites changed during registration")
        (staging / "manifest.json").write_bytes(canonical_json_bytes({
            "schema": SITES_SCHEMA, "sha256": digest, "size_bytes": size,
            "source_label": source_label,
        }))

    store.publish_directory(directory.parent, directory.name, build)
    return inspect_sites(store)
