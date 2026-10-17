"""Operator-pinned Kraken2/Bracken database without copying a large DB."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained, _regular_source
from .file_access import open_regular_no_follow

if TYPE_CHECKING:
    from .store import LabStore

SCHEMA = "traceback.lab.microbial-db.v2"
LEGACY_SCHEMA = "traceback.lab.microbial-db.v1"
CORE = ("hash.k2d", "opts.k2d", "taxo.k2d")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class MicrobialDbError(ValueError):
    """The database registration is missing, unsafe, or changed."""


@dataclass(frozen=True)
class MicrobialDb:
    directory: Path
    read_length: int
    source_label: str
    sha256: str
    files: dict[str, str]

    @property
    def bracken_distribution(self) -> Path:
        return self.directory / f"database{self.read_length}mers.kmer_distrib"


def _files(read_length: int) -> tuple[str, ...]:
    if not 75 <= read_length <= 500:
        raise MicrobialDbError("Bracken read length must be 75 to 500 bases")
    return (*CORE, f"database{read_length}mers.kmer_distrib")


def _stat_receipt(info: os.stat_result) -> dict[str, int]:
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "dev": info.st_dev, "ino": info.st_ino}


def _file_receipt(path: Path) -> dict[str, int]:
    try:
        descriptor = open_regular_no_follow(path)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise MicrobialDbError("database file must be nonempty and regular")
            path_info = path.lstat()
            if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
                raise MicrobialDbError("database file changed during verification")
            return _stat_receipt(info)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MicrobialDbError("database file is missing or unsafe") from exc


def _digest_file(path: Path) -> tuple[str, dict[str, int]]:
    try:
        descriptor = open_regular_no_follow(path)
    except OSError as exc:
        raise MicrobialDbError("database file is missing or unsafe") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise MicrobialDbError("database file must be nonempty and regular")
        while block := os.read(descriptor, 1 << 20):
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise MicrobialDbError("database file changed during verification")
        path_after = path.lstat()
        if (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino):
            raise MicrobialDbError("database file changed during verification")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), _stat_receipt(after)


def _safe_label(label: str) -> bool:
    return (1 <= len(label) <= 160 and label == label.strip()
            and not any(char in label for char in ("/", "\\"))
            and not any(ord(char) < 32 for char in label))


def _directory(store: LabStore) -> Path:
    return store.lab / "assets" / "microbial-db"


def inspect_database(store: LabStore, *, verify_bytes: bool = True) -> MicrobialDb:
    manifest = _directory(store) / "manifest.json"
    try:
        _check_contained(store, manifest)
        with os.fdopen(open_regular_no_follow(manifest), "rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("manifest too large")
        data = json.loads(raw)
    except (OSError, ValueError, AssetError) as exc:
        raise MicrobialDbError("microbial database registration is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema") not in (SCHEMA, LEGACY_SCHEMA):
        raise MicrobialDbError("microbial database registration is invalid")
    legacy = data["schema"] == LEGACY_SCHEMA
    fields = {"schema", "directory", "read_length", "source_label", "files", "sha256"}
    if not legacy:
        fields.add("file_stats")
    if (set(data) != fields or not isinstance(data["directory"], str)
            or not isinstance(data["read_length"], int) or isinstance(data["read_length"], bool)
            or not isinstance(data["source_label"], str) or not _safe_label(data["source_label"])
            or not isinstance(data["sha256"], str) or not SHA.fullmatch(data["sha256"])):
        raise MicrobialDbError("microbial database registration is invalid")
    names = _files(data["read_length"])
    if (not isinstance(data["files"], dict) or set(data["files"]) != set(names)
            or any(not isinstance(value, str) or not SHA.fullmatch(value) for value in data["files"].values())):
        raise MicrobialDbError("microbial database file manifest is invalid")
    if not legacy:
        if (not isinstance(data["file_stats"], dict) or set(data["file_stats"]) != set(names)
                or any(not isinstance(item, dict) or set(item) != {"size", "mtime_ns", "ctime_ns", "dev", "ino"}
                       or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                              for value in item.values()) for item in data["file_stats"].values())):
            raise MicrobialDbError("microbial database verification receipt is invalid")
    directory = Path(data["directory"])
    if not directory.is_absolute() or directory.is_symlink():
        raise MicrobialDbError("registered database path is unsafe")
    if legacy and not verify_bytes:
        raise MicrobialDbError("legacy database registration needs verified re-registration")
    try:
        if verify_bytes:
            checked = {name: _digest_file(_regular_source(directory / name)) for name in names}
            files = {name: item[0] for name, item in checked.items()}
        else:
            receipts = {name: _file_receipt(_regular_source(directory / name)) for name in names}
            if receipts != data["file_stats"]:
                raise MicrobialDbError("registered database files changed since byte verification")
            files = data["files"]
    except (OSError, AssetError) as exc:
        raise MicrobialDbError("registered database files are unavailable or unsafe") from exc
    bundle = hashlib.sha256(canonical_json_bytes({"read_length": data["read_length"], "files": files})).hexdigest()
    if files != data["files"] or bundle != data["sha256"]:
        raise MicrobialDbError("registered database bytes changed")
    return MicrobialDb(directory, data["read_length"], data["source_label"], bundle, files)


def register_database(store: LabStore, directory: Path, read_length: int, source_label: str) -> MicrobialDb:
    """Record exact operator-owned DB bytes under the held Lab writer lock."""

    if not _safe_label(source_label):
        raise MicrobialDbError("source label must be a short description, not a path")
    names = _files(read_length)
    directory = Path(os.path.abspath(directory))
    try:
        checked = {name: _digest_file(_regular_source(directory / name)) for name in names}
        files = {name: item[0] for name, item in checked.items()}
        file_stats = {name: item[1] for name, item in checked.items()}
    except (OSError, AssetError) as exc:
        raise MicrobialDbError("database directory must contain regular Kraken2 and Bracken files") from exc
    bundle = hashlib.sha256(canonical_json_bytes({"read_length": read_length, "files": files})).hexdigest()
    destination = _directory(store)
    try:
        _check_contained(store, destination)
    except AssetError as exc:
        raise MicrobialDbError("database registration path is unsafe") from exc
    if destination.exists() or destination.is_symlink():
        existing = inspect_database(store)
        if existing.sha256 != bundle or existing.source_label != source_label:
            raise MicrobialDbError("a different microbial database is already registered")
        if existing.directory != directory:
            raise MicrobialDbError("the same database bytes are already registered at a different directory")
        from .store import write_atomic

        try:
            write_atomic(destination / "manifest.json", canonical_json_bytes({
                "schema": SCHEMA, "directory": str(directory), "read_length": read_length,
                "source_label": source_label, "files": files, "file_stats": file_stats, "sha256": bundle,
            }))
        except OSError as exc:
            raise MicrobialDbError("database registration could not be updated") from exc
        return inspect_database(store)

    def build(staging: Path) -> None:
        (staging / "manifest.json").write_bytes(canonical_json_bytes({
            "schema": SCHEMA, "directory": str(directory), "read_length": read_length,
            "source_label": source_label, "files": files, "file_stats": file_stats, "sha256": bundle,
        }))

    store.publish_directory(destination.parent, destination.name, build)
    return inspect_database(store)
