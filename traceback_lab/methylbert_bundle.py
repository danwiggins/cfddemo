"""Validate and privately unpack a trusted MethylBERT tumour model bundle.

The model includes pickle files. Registration verifies bytes and shape, but
loading the model is code execution and must happen only for an operator-
trusted bundle inside the isolated Lab worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = "traceback.methylbert-bundle.v1"
REQUIRED_FILES = frozenset({
    "bert.model/config.json",
    "bert.model/pytorch_model.bin",
    "bert.model/dmr_encoder.pickle",
    "bert.model/read_classification_model.pickle",
    "train_seq.csv",
    "dmrs.tsv",
})
MAX_FILE_BYTES = 2 * 1024**3


class BundleError(ValueError):
    """The registered archive cannot be used as a MethylBERT model bundle."""


@dataclass(frozen=True)
class Bundle:
    model_id: str
    upstream_commit: str
    source: str
    terms: str
    reference_sha256: str
    n_mers: int
    seq_len: int
    files: dict[str, str]


def _sha256(stream: Any, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(16 << 20, remaining))
        if not chunk:
            raise BundleError("a bundle file ended before its declared size")
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise BundleError("a bundle file exceeds its declared size")
    return digest.hexdigest()


def _manifest(data: bytes) -> Bundle:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError("bundle.json is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema", "model_id", "upstream_commit", "source", "terms", "reference_sha256",
        "n_mers", "seq_len", "files",
    } or raw["schema"] != SCHEMA:
        raise BundleError("bundle.json has an unsupported schema or fields")
    for field in ("model_id", "source", "terms"):
        if not isinstance(raw[field], str) or not raw[field].strip() or len(raw[field]) > 500:
            raise BundleError(f"bundle.json needs a nonempty {field}")
    for field in ("upstream_commit", "reference_sha256"):
        value = raw[field]
        length = 40 if field == "upstream_commit" else 64
        if not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value):
            raise BundleError(f"bundle.json has an invalid {field}")
    if (type(raw["n_mers"]) is not int or raw["n_mers"] not in (3,)
            or type(raw["seq_len"]) is not int or not 1 <= raw["seq_len"] <= 4096):
        raise BundleError("bundle.json has unsupported tokenizer settings")
    files = raw["files"]
    if not isinstance(files, dict) or set(files) != REQUIRED_FILES:
        raise BundleError("bundle.json must hash every required model and preprocessing file")
    if any(not isinstance(value, str) or len(value) != 64
           or any(c not in "0123456789abcdef" for c in value) for value in files.values()):
        raise BundleError("bundle.json has an invalid file SHA-256")
    return Bundle(
        model_id=raw["model_id"], upstream_commit=raw["upstream_commit"],
        source=raw["source"], terms=raw["terms"],
        reference_sha256=raw["reference_sha256"], n_mers=raw["n_mers"],
        seq_len=raw["seq_len"], files=dict(files),
    )


def verify_bundle(path: Path) -> Bundle:
    """Require one uncompressed TAR with exact members and matching file hashes."""

    try:
        with tarfile.open(path, mode="r:") as archive:
            members = archive.getmembers()
            by_name = {member.name: member for member in members}
            if len(by_name) != len(members) or set(by_name) != REQUIRED_FILES | {"bundle.json"}:
                raise BundleError("bundle has missing, extra, or duplicate members")
            if any(not member.isfile() or member.size < 1 or member.size > MAX_FILE_BYTES
                   for member in members):
                raise BundleError("bundle members must be bounded regular files")
            manifest_member = by_name["bundle.json"]
            if manifest_member.size > 64 * 1024:
                raise BundleError("bundle.json is too large")
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise BundleError("bundle.json is unreadable")
            with manifest_stream:
                bundle = _manifest(manifest_stream.read())
            for name, expected in bundle.files.items():
                stream = archive.extractfile(by_name[name])
                if stream is None:
                    raise BundleError(f"{name} is unreadable")
                with stream:
                    if _sha256(stream, by_name[name].size) != expected:
                        raise BundleError(f"{name} differs from bundle.json")
            return bundle
    except (OSError, tarfile.TarError) as exc:
        raise BundleError("bundle is not a readable uncompressed TAR") from exc


def unpack_bundle(path: Path, destination: Path) -> Bundle:
    """Reverify and copy fixed members into a new private directory."""

    bundle = verify_bundle(path)
    if destination.exists() or destination.is_symlink():
        raise BundleError("bundle destination must not exist")
    destination.mkdir(mode=0o700, parents=True)
    with tarfile.open(path, mode="r:") as archive:
        for name in sorted(REQUIRED_FILES):
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            member = archive.getmember(name)
            source = archive.extractfile(member)
            if source is None:
                raise BundleError(f"{name} is unreadable")
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
            )
            with source, os.fdopen(descriptor, "wb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(16 << 20, remaining))
                    if not chunk:
                        raise BundleError(f"{name} ended during private copy")
                    output.write(chunk)
                    remaining -= len(chunk)
            with target.open("rb") as copied:
                if _sha256(copied, member.size) != bundle.files[name]:
                    raise BundleError(f"{name} changed during private copy")
    return bundle
