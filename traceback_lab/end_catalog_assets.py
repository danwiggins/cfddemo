"""Pinned, operator-supplied hg38 catalogues for exploratory aligned-end analysis."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from .file_access import open_regular_no_follow


SCHEMA = "traceback.lab.end-catalogues.v1"
MAX_CATALOGUE_BYTES = 64 << 20
MAX_MANIFEST_BYTES = 8192
_CHROM = re.compile(r"chr(?:[1-9]|1[0-9]|2[0-2])$")
_HEX = re.compile(r"[0-9a-f]{64}$")


class EndCatalogueError(ValueError):
    """A catalogue is absent, unsafe, malformed or changed."""


@dataclass(frozen=True)
class EndCatalogues:
    identity: str
    centers_sha256: str
    preferred_sha256: str
    centers: dict[str, np.ndarray]
    preferred_u: dict[str, np.ndarray]
    preferred_d: dict[str, np.ndarray]
    center_count: int
    preferred_count: int
    source_label: str
    centers_source: str
    preferred_source: str
    terms: str


def _location(root: Path) -> Path:
    return root / "lab" / "assets" / "aligned-ends" / "registration.json"


def _read_regular(path: Path, limit: int) -> bytes:
    """Read one bounded regular inode; canonicalize platform symlink parents."""

    absolute = Path(os.path.abspath(path))
    if absolute.is_symlink():
        raise EndCatalogueError("catalogue file is a symlink")
    try:
        canonical = absolute.resolve(strict=True)
        with os.fdopen(open_regular_no_follow(canonical), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise EndCatalogueError("catalogue is not a bounded regular file")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = os.lstat(canonical)
        current_alias = os.lstat(absolute)
    except (OSError, RuntimeError) as exc:
        raise EndCatalogueError("catalogue is absent, unreadable or changed") from exc
    def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if (len(raw) > limit or not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(current_alias.st_mode)
            or identity(before) != identity(after) or identity(after) != identity(current)
            or identity(current) != identity(current_alias)):
        raise EndCatalogueError("catalogue changed or exceeds its size limit")
    return raw


def _parse_rows(raw: bytes, *, preferred: bool) -> dict[str, np.ndarray] | tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Strict, sorted 0-based BED3 centers or BED4 one-base U/D end sites."""

    rows: dict[str, list[int]] = {}
    ends: dict[str, list[int]] = {}
    prior: tuple[int, int] | None = None
    labels_at_position: set[str] = set()
    try:
        lines = raw.decode("ascii").splitlines()
        for line in lines:
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != (4 if preferred else 3) or not _CHROM.fullmatch(fields[0]):
                raise ValueError
            chrom = fields[0]
            if any(not field.isdigit() for field in fields[1:3]):
                raise ValueError
            start, end = int(fields[1]), int(fields[2])
            if not (0 <= start < end < 2**31):
                raise ValueError
            if preferred:
                if end != start + 1 or fields[3] not in {"U", "D"}:
                    raise ValueError
                position = start
            else:
                position = (start + end) // 2
            key = (int(chrom[3:]), position)
            if prior is not None and key < prior:
                raise ValueError
            if key != prior:
                labels_at_position.clear()
            if key == prior and (not preferred or fields[3] in labels_at_position):
                raise ValueError
            prior = key
            if preferred:
                labels_at_position.add(fields[3])
                (rows if fields[3] == "U" else ends).setdefault(chrom, []).append(position)
            else:
                values = rows.setdefault(chrom, [])
                if values and position - values[-1] <= 200:
                    raise ValueError  # ±100 bp windows must not overlap.
                values.append(position)
    except (UnicodeError, ValueError) as exc:
        raise EndCatalogueError("catalogue must be sorted hg38 BED with valid nonoverlapping coordinates") from exc
    if not prior:
        raise EndCatalogueError("catalogue has no valid sites")
    def make(values: dict[str, list[int]]) -> dict[str, np.ndarray]:
        return {chrom: np.asarray(points, dtype=np.int64) for chrom, points in values.items()}
    return (make(rows), make(ends)) if preferred else make(rows)


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _valid_source_label(label: object, *, identifier: bool = False) -> bool:
    if (not isinstance(label, str) or not label or len(label) > 160 or label != label.strip()
            or "\\" in label or any(ord(char) < 32 or 0x202a <= ord(char) <= 0x202e for char in label)):
        return False
    if "/" not in label:
        return True
    if not identifier:
        return False
    doi = label.removeprefix("doi:")
    if re.fullmatch(r"10\.\d{4,9}/[^\s\\]+", doi):
        return True
    try:
        parsed = urlsplit(label)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _verified_bytes(data: dict) -> tuple[bytes, bytes]:
    center_raw = _read_regular(Path(data["centers_path"]), MAX_CATALOGUE_BYTES)
    preferred_raw = _read_regular(Path(data["preferred_path"]), MAX_CATALOGUE_BYTES)
    if hashlib.sha256(center_raw).hexdigest() != data["centers_sha256"] or hashlib.sha256(preferred_raw).hexdigest() != data["preferred_sha256"]:
        raise EndCatalogueError("registered catalogue bytes changed")
    return center_raw, preferred_raw


def _load_catalogues(data: dict, center_raw: bytes, preferred_raw: bytes) -> EndCatalogues:
    centers = _parse_rows(center_raw, preferred=False)
    preferred_u, preferred_d = _parse_rows(preferred_raw, preferred=True)
    return EndCatalogues(
        identity=data["identity"], centers_sha256=data["centers_sha256"],
        preferred_sha256=data["preferred_sha256"], centers=centers,
        preferred_u=preferred_u, preferred_d=preferred_d,
        center_count=sum(map(len, centers.values())),
        preferred_count=sum(map(len, preferred_u.values())) + sum(map(len, preferred_d.values())),
        source_label=data["source_label"], centers_source=data["centers_source"],
        preferred_source=data["preferred_source"], terms=data["terms"],
    )


def _read_registration(root: Path) -> dict:
    raw = _read_regular(_location(root), MAX_MANIFEST_BYTES)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {
            "schema", "assembly", "centers_path", "centers_sha256", "preferred_path", "preferred_sha256",
            "source_label", "centers_source", "preferred_source", "terms", "license_id", "identity",
        } or data["schema"] != SCHEMA or data["assembly"] != "hg38":
            raise ValueError
        identity = data.pop("identity")
        if not isinstance(identity, str) or not _HEX.fullmatch(identity) or identity != hashlib.sha256(_canonical(data)).hexdigest():
            raise ValueError
        if any(not isinstance(data[key], str) or not _HEX.fullmatch(data[key]) for key in ("centers_sha256", "preferred_sha256")):
            raise ValueError
        if (any(not isinstance(data[key], str) or not Path(data[key]).is_absolute()
                for key in ("centers_path", "preferred_path"))
                or any(not _valid_source_label(data[key], identifier=key != "source_label")
                       for key in ("source_label", "centers_source", "preferred_source"))
                or not isinstance(data["terms"], str)
                or data["terms"] not in {"known", "restricted", "unknown"}
                or (data["terms"] == "known" and not data["license_id"])
                or (data["license_id"] is not None and
                    (not isinstance(data["license_id"], str) or
                     not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", data["license_id"])))):
            raise ValueError
        data["identity"] = identity
    except (KeyError, TypeError, ValueError) as exc:
        raise EndCatalogueError("end catalogue registration is invalid") from exc
    return data


def end_catalogue_identity(root: Path) -> str:
    """Rehash pinned bytes for cache identity without parsing millions of rows."""

    data = _read_registration(root)
    _verified_bytes(data)
    return data["identity"]


def inspect_end_catalogues(root: Path) -> EndCatalogues:
    data = _read_registration(root)
    return _load_catalogues(data, *_verified_bytes(data))


def register_end_catalogues(
    root: Path, *, centers: Path, preferred: Path, source_label: str,
    centers_source: str, preferred_source: str, terms: str, license_id: str | None,
) -> EndCatalogues:
    for label, identifier in ((source_label, False), (centers_source, True), (preferred_source, True)):
        if not _valid_source_label(label, identifier=identifier):
            raise EndCatalogueError("source labels must be short descriptions, not paths")
    if terms not in {"known", "restricted", "unknown"} or (terms == "known" and not license_id):
        raise EndCatalogueError("valid terms and license ID are required")
    if license_id is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", license_id):
        raise EndCatalogueError("invalid license ID")
    center_raw = _read_regular(centers, MAX_CATALOGUE_BYTES)
    preferred_raw = _read_regular(preferred, MAX_CATALOGUE_BYTES)
    try:
        center_path = centers.resolve(strict=True)
        preferred_path = preferred.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EndCatalogueError("catalogue is absent, unreadable or changed") from exc
    data = {
        "schema": SCHEMA, "assembly": "hg38", "centers_path": str(center_path),
        "centers_sha256": hashlib.sha256(center_raw).hexdigest(),
        "preferred_path": str(preferred_path),
        "preferred_sha256": hashlib.sha256(preferred_raw).hexdigest(),
        "source_label": source_label, "centers_source": centers_source,
        "preferred_source": preferred_source, "terms": terms, "license_id": license_id,
    }
    data["identity"] = hashlib.sha256(_canonical(data)).hexdigest()
    encoded = _canonical(data)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise EndCatalogueError("end catalogue registration exceeds its size limit")
    prepared = _load_catalogues(data, center_raw, preferred_raw)
    location = _location(root)
    if any(parent.is_symlink() for parent in (root, location.parent.parent.parent, location.parent.parent, location.parent)):
        raise EndCatalogueError("Lab asset directory contains a symlink")
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".aligned-ends-", dir=location.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, location)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as exc:
        raise EndCatalogueError("end catalogue registration could not be published") from exc
    return prepared
