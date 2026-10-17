"""Operator-pinned hg38 coordinates for the published three-CpG Weidner clock.

The coefficient table is methylclockData's coefWeidner.rda (Zenodo record
22069904, SHA-256 below). This module does not redistribute that R asset;
the four published numeric coefficients are fixed in the algorithm. The
operator supplies only the assay-site-to-hg38 mapping, never model weights.
The paper's third coefficient measures the PDE4C CpG upstream of the
cg17861230 array probe, although the coefficient asset labels it cg17861230.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained, _regular_source
from .file_access import open_regular_no_follow

if TYPE_CHECKING:
    from .store import LabStore

SCHEMA = "traceback.lab.epi-clock-map.v2"
MODEL_ID = "Weidner-3"
COEFFICIENTS_SHA256 = "9a3feb7d0c500f9ccdb61ffe243775587fa21b082536142bb1260c566cd39645"
# Weidner equation: alpha=ASPA/cg02228185, beta=ITGA2B/cg25809905,
# gamma=PDE4C assay CpG upstream of the cg17861230 array probe.
WEIDNER_SITES = ("cg02228185", "cg25809905", "pde4c_upstream_of_cg17861230")
WEIDNER_WEIGHTS = (-26.4, -23.7, 164.7)
WEIDNER_INTERCEPT = 38.0
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CHROM = re.compile(r"chr(?:[1-9]|1[0-9]|2[0-2])\Z")


class ClockModelError(ValueError):
    """The coordinate asset is absent, changed, or incompatible."""


@dataclass(frozen=True)
class ClockMap:
    sha256: str
    source_label: str
    sites: tuple[tuple[str, str, int], ...]


def _parse(raw: bytes, digest: str) -> ClockMap:
    if len(raw) > 8192:
        raise ClockModelError("clock coordinate file exceeds 8 KiB")
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ClockModelError("clock coordinate file is invalid JSON") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "model_id", "genome_build", "source_label", "sites"}
            or data["schema"] != SCHEMA or data["model_id"] != MODEL_ID or data["genome_build"] != "hg38"
            or not isinstance(data["source_label"], str) or not 1 <= len(data["source_label"]) <= 160
            or any(ord(char) < 32 or char in "/\\" for char in data["source_label"]) or
            not isinstance(data["sites"], list) or len(data["sites"]) != 3):
        raise ClockModelError("clock map requires a named hg38 Weidner-3 source and exactly three sites")
    by_id: dict[str, tuple[str, str, int]] = {}
    positions: set[tuple[str, int]] = set()
    for row in data["sites"]:
        if (not isinstance(row, dict) or set(row) != {"cpg_id", "chrom", "pos0"}
                or not isinstance(row["cpg_id"], str) or row["cpg_id"] not in WEIDNER_SITES
                or not isinstance(row["chrom"], str) or not _CHROM.fullmatch(row["chrom"])
                or not isinstance(row["pos0"], int) or isinstance(row["pos0"], bool)
                or row["pos0"] < 0 or row["pos0"] >= 250_000_000):
            raise ClockModelError("clock map has an invalid assay site or hg38 C coordinate")
        key = (row["chrom"], row["pos0"])
        if row["cpg_id"] in by_id or key in positions:
            raise ClockModelError("clock map repeats an assay site or position")
        by_id[row["cpg_id"]] = (row["cpg_id"], *key)
        positions.add(key)
    if set(by_id) != set(WEIDNER_SITES):
        raise ClockModelError("clock map must contain the three published Weidner assay sites")
    return ClockMap(digest, data["source_label"], tuple(by_id[site] for site in WEIDNER_SITES))


def _read_regular(path: Path) -> bytes:
    try:
        from os import close, fstat, read

        descriptor = open_regular_no_follow(path)
        try:
            size = fstat(descriptor).st_size
            if size <= 0 or size > 8192:
                raise ClockModelError("clock coordinate file must be 1 to 8192 bytes")
            data = read(descriptor, 8193)
            if len(data) != size or fstat(descriptor).st_size != size:
                raise ClockModelError("clock coordinate file changed while reading")
            return data
        finally:
            close(descriptor)
    except OSError as exc:
        raise ClockModelError("clock coordinate file is missing or unsafe") from exc


def _directory(store: LabStore) -> Path:
    return store.lab / "assets" / "epi-clock-map"


def inspect_map(store: LabStore) -> ClockMap:
    directory = _directory(store)
    try:
        _check_contained(store, directory)
        receipt = json.loads(_read_regular(directory / "receipt.json"))
        if (not isinstance(receipt, dict) or set(receipt) != {"schema", "sha256"}
                or receipt["schema"] != SCHEMA or not isinstance(receipt["sha256"], str)
                or not _SHA.fullmatch(receipt["sha256"])):
            raise ClockModelError("clock map receipt is invalid")
        raw = _read_regular(directory / "coordinates.json")
    except (OSError, ValueError, AssetError, RecursionError) as exc:
        raise ClockModelError("clock map registration is missing or unsafe") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != receipt["sha256"]:
        raise ClockModelError("clock coordinate bytes changed")
    return _parse(raw, digest)


def register_map(store: LabStore, source: Path, expected_sha256: str) -> ClockMap:
    """Called under the Lab writer lock; copied bytes are immutable thereafter."""

    if not _SHA.fullmatch(expected_sha256):
        raise ClockModelError("expected SHA-256 must be 64 lowercase hex characters")
    try:
        raw = _read_regular(_regular_source(source))
    except AssetError as exc:
        raise ClockModelError("clock coordinate source must be a regular file") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ClockModelError("clock coordinate source differs from expected SHA-256")
    _parse(raw, digest)
    destination = _directory(store)
    try:
        _check_contained(store, destination)
    except AssetError as exc:
        raise ClockModelError("clock map destination is unsafe") from exc
    if destination.exists() or destination.is_symlink():
        current = inspect_map(store)
        if current.sha256 != digest:
            raise ClockModelError("a different clock map is already registered")
        return current

    def build(staging: Path) -> None:
        (staging / "coordinates.json").write_bytes(raw)
        (staging / "receipt.json").write_bytes(canonical_json_bytes({"schema": SCHEMA, "sha256": digest}))

    try:
        store.publish_directory(destination.parent, destination.name, build)
    except OSError as exc:
        raise ClockModelError("clock map could not be published; inspect registration before retrying") from exc
    return inspect_map(store)
