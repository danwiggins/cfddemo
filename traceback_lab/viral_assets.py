"""Operator-supplied, immutable viral side-alignment reference (D10).

The Lab never downloads or redistributes a viral reference. A registration
names every FASTA contig, its EBV/HPV/HBV group, and its accession/version;
the exact FASTA and mapping bytes enter the execution identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from traceback_runner.serialization import canonical_json_bytes

from .asset_contracts import AssetDescriptor
from .assets import AssetError, _check_contained, _digest, _open_regular, _regular_source

if TYPE_CHECKING:
    from .store import LabStore

KIND = "viral-fasta"
FASTA_NAME = "viral.fasta"
TARGETS_NAME = "targets.tsv"
MANIFEST_NAME = "manifest.json"
SCHEMA = "traceback.lab.viral-reference.v1"
VIRUSES = ("EBV", "HPV", "HBV")
MAX_FASTA_BYTES = 64 << 20
MAX_TARGETS_BYTES = 256 << 10
_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_IUPAC = frozenset("ACGTRYSWKMBDHVN")


@dataclass(frozen=True)
class ViralReference:
    descriptor: AssetDescriptor
    fasta: Path
    targets_sha256: str
    targets: tuple[tuple[str, str, str], ...]

    @property
    def asset_id(self) -> str:
        return self.descriptor.asset_id

    @property
    def sha256(self) -> str:
        return self.descriptor.sha256

    @property
    def file(self) -> Path:
        return self.fasta

    @property
    def asset_sha256s(self) -> tuple[str, str]:
        return (self.descriptor.sha256, self.targets_sha256)

    def public(self) -> dict[str, Any]:
        return {
            **self.descriptor.model_dump(by_alias=True),
            "asset_id": self.descriptor.asset_id,
            "file": str(self.fasta),
            "targets_sha256": self.targets_sha256,
            "targets": [
                {"contig": contig, "virus": virus, "accession": accession}
                for contig, virus, accession in self.targets
            ],
        }


def _directory(store: LabStore) -> Path:
    return store.lab / "assets" / KIND


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        with os.fdopen(_open_regular(path), "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise AssetError("viral reference file is absent or unreadable") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise AssetError("viral reference file changed while it was read")
    if len(raw) > limit:
        raise AssetError("viral reference file exceeds its size budget")
    return raw


def _fasta_contigs(raw: bytes) -> tuple[str, ...]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise AssetError("viral FASTA must be ASCII") from exc
    names: list[str] = []
    bases = 0
    for line in lines:
        if not line:
            continue
        if line.startswith(">"):
            if names and bases == 0:
                raise AssetError("viral FASTA contains an empty sequence")
            words = line[1:].split(maxsplit=1)
            name = words[0] if words else ""
            if not _NAME.fullmatch(name) or name in names:
                raise AssetError("viral FASTA contig names must be distinct and simple")
            names.append(name)
            bases = 0
        elif line and names and all(base in _IUPAC for base in line.upper()):
            bases += len(line)
        else:
            raise AssetError("viral FASTA contains invalid sequence data")
    if not names or bases == 0:
        raise AssetError("viral FASTA needs nonempty contigs")
    return tuple(names)


def _target_rows(raw: bytes, contigs: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise AssetError("viral target map must be ASCII") from exc
    if not lines or lines[0] != "contig\tvirus\taccession" or len(lines) > 10001:
        raise AssetError("viral target map needs its header and at most 10,000 rows")
    rows: list[tuple[str, str, str]] = []
    for line in lines[1:]:
        fields = tuple(line.split("\t"))
        if len(fields) != 3 or any(not _NAME.fullmatch(field) for field in fields) or fields[1] not in VIRUSES:
            raise AssetError("viral target map needs contig, EBV/HPV/HBV, accession")
        rows.append((fields[0], fields[1], fields[2]))
    if tuple(row[0] for row in rows) != contigs or set(row[1] for row in rows) != set(VIRUSES):
        raise AssetError("viral target map must cover every FASTA contig and all three viruses")
    return tuple(rows)


def _descriptor(sha256: str, size: int, source_label: str, terms: str, license_id: str | None) -> AssetDescriptor:
    if (source_label != source_label.strip() or any(char in source_label for char in ("/", "\\"))
            or any(ord(char) < 32 for char in source_label)):
        raise AssetError("source label must describe provenance, not a path")
    try:
        return AssetDescriptor(
            kind=KIND, sha256=sha256, size_bytes=size, origin="user",
            source_label=source_label, terms=terms, license_id=license_id,
        )
    except ValidationError as exc:
        raise AssetError("invalid viral source or terms metadata") from exc


def inspect_viral_reference(store: LabStore) -> ViralReference:
    directory = _directory(store)
    fasta = directory / FASTA_NAME
    targets = directory / TARGETS_NAME
    manifest = directory / MANIFEST_NAME
    for path in (directory, fasta, targets, manifest):
        _check_contained(store, path)
    fasta_sha, fasta_size = _digest(fasta)
    targets_sha, _ = _digest(targets)
    try:
        metadata = json.loads(_read_regular(manifest, 65536))
        if set(metadata) != {"schema", "descriptor", "file", "targets_file", "targets_sha256"}:
            raise ValueError("unexpected viral manifest fields")
        if (metadata["schema"] != SCHEMA or metadata["file"] != FASTA_NAME
                or metadata["targets_file"] != TARGETS_NAME or metadata["targets_sha256"] != targets_sha):
            raise ValueError("viral manifest file identity mismatch")
        descriptor = AssetDescriptor.model_validate(metadata["descriptor"])
    except (OSError, ValueError, TypeError, KeyError, ValidationError) as exc:
        raise AssetError("viral reference manifest is invalid") from exc
    if (descriptor.kind != KIND or descriptor.origin != "user"
            or descriptor.sha256 != fasta_sha or descriptor.size_bytes != fasta_size):
        raise AssetError("viral reference bytes differ from registration")
    fasta_raw = _read_regular(fasta, MAX_FASTA_BYTES)
    targets_raw = _read_regular(targets, MAX_TARGETS_BYTES)
    if (hashlib.sha256(fasta_raw).hexdigest() != fasta_sha
            or hashlib.sha256(targets_raw).hexdigest() != targets_sha):
        raise AssetError("viral reference changed while it was inspected")
    contigs = _fasta_contigs(fasta_raw)
    rows = _target_rows(targets_raw, contigs)
    return ViralReference(descriptor, fasta, targets_sha, rows)


def add_viral_reference(
    store: LabStore, fasta: Path, targets: Path, *, source_label: str,
    terms: str, license_id: str | None = None,
) -> ViralReference:
    """Atomically register one immutable bundle under the held Lab writer lock."""

    fasta = _regular_source(fasta)
    targets = _regular_source(targets)
    fasta_raw = _read_regular(fasta, MAX_FASTA_BYTES)
    targets_raw = _read_regular(targets, MAX_TARGETS_BYTES)
    contigs = _fasta_contigs(fasta_raw)
    _target_rows(targets_raw, contigs)
    fasta_sha, fasta_size = _digest(fasta)
    targets_sha, _ = _digest(targets)
    if fasta_sha != hashlib.sha256(fasta_raw).hexdigest() or targets_sha != hashlib.sha256(targets_raw).hexdigest():
        raise AssetError("viral source changed while it was read")
    descriptor = _descriptor(fasta_sha, fasta_size, source_label, terms, license_id)
    directory = _directory(store)
    _check_contained(store, directory)
    if directory.exists() or directory.is_symlink():
        existing = inspect_viral_reference(store)
        if existing.descriptor != descriptor or existing.targets_sha256 != targets_sha:
            raise AssetError("a different viral reference is already registered under this ROOT")
        return existing

    def build(staging: Path) -> None:
        (staging / FASTA_NAME).write_bytes(fasta_raw)
        (staging / TARGETS_NAME).write_bytes(targets_raw)
        (staging / MANIFEST_NAME).write_bytes(canonical_json_bytes({
            "schema": SCHEMA,
            "descriptor": descriptor.model_dump(by_alias=True),
            "file": FASTA_NAME,
            "targets_file": TARGETS_NAME,
            "targets_sha256": targets_sha,
        }))

    try:
        store.publish_directory(directory.parent, KIND, build)
    except FileExistsError as exc:
        raise AssetError("viral reference was registered concurrently; inspect before retry") from exc
    return inspect_viral_reference(store)


__all__ = ["KIND", "VIRUSES", "ViralReference", "add_viral_reference", "inspect_viral_reference"]
