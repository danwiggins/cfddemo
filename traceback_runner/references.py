"""Local reference registration for unqualified real-BAM preflight.

A registration records the identity of one FASTA under
``ROOT/references/<id>/``. The FASTA itself is referenced, never copied.

``registered-reference.json`` holds the canonical ``RegisteredReference``
(contig names, lengths and SAM ``M5`` digests plus the FASTA SHA-256).
``source.json`` holds the local FASTA locator and size. The locator is local
operator state: it must never be copied into a bundle, catalog row or explorer
artifact.

Registration does not qualify the reference or any result made against it.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from .contracts import ReferenceContig, RegisteredReference, RunnerContract
from .filesystem import rename_directory_exclusive_at
from .serialization import canonical_json_bytes, canonical_model_from_bytes

REFERENCES_DIRECTORY = "references"
REGISTRATION_FILE = "registered-reference.json"
SOURCE_FILE = "source.json"
REFERENCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DOCS_ANCHOR = "docs/OPERATOR-GUIDE.md#real-local-bam-unqualified"

_CHUNK_BYTES = 16 * 1024 * 1024
_MAX_FAI_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 64 * 1024 * 1024
_WHITESPACE = b" \t\r\n\v\f"
_UPPERCASE = bytes.maketrans(
    b"abcdefghijklmnopqrstuvwxyz", b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
_CONTIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ReferenceProblem(Exception):
    """A reference command failed with a stable, operator-facing problem."""

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        cause: str,
        fix: str,
        exit_code: int = 3,
        retryable: bool = False,
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.cause = cause
        self.fix = fix
        self.exit_code = exit_code
        self.retryable = retryable


class ReferenceSource(RunnerContract):
    """Local-only locator for a registered FASTA; never exported."""

    schema_version: Literal["traceback.reference-source.v1"] = "traceback.reference-source.v1"
    fasta_path: str = Field(min_length=1, max_length=4096)
    fasta_size_bytes: int = Field(ge=1)
    assembly_declared: bool


@dataclass(frozen=True)
class LoadedReference:
    registered: RegisteredReference
    source: ReferenceSource


@dataclass(frozen=True)
class RegistrationResult:
    registered: RegisteredReference
    created: bool


def validate_reference_id(value: str) -> str:
    """Return ``value`` when it is a safe single directory name."""

    if not REFERENCE_ID_PATTERN.fullmatch(value) or ".." in value:
        raise ValueError(
            "reference ID must match ^[a-z0-9][a-z0-9._-]{0,63}$ and must not contain '..'"
        )
    return value


def references_root(root: Path) -> Path:
    return Path(root) / REFERENCES_DIRECTORY


def _fai_problem(cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-REF-001",
        "FASTA index is missing or contradicts the FASTA",
        cause=cause,
        fix="Run `samtools faidx` on the uncompressed FASTA, then register again",
    )


def _fasta_problem(cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-REF-001",
        "FASTA cannot be registered",
        cause=cause,
        fix="Supply an uncompressed FASTA with a matching `.fai` index",
    )


def read_fai(path: Path) -> tuple[tuple[str, int], ...]:
    """Return ``(name, length)`` rows from a samtools ``.fai`` index."""

    try:
        if path.stat().st_size > _MAX_FAI_BYTES:
            raise _fai_problem("FASTA index is implausibly large")
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise _fai_problem("no `.fai` index next to the FASTA") from exc
    rows: list[tuple[str, int]] = []
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _fai_problem("FASTA index is not ASCII text") from exc
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) not in {5, 6} or not fields[1].isdigit():
            raise _fai_problem("FASTA index row is malformed")
        rows.append((fields[0], int(fields[1])))
    if not rows:
        raise _fai_problem("FASTA index is empty")
    return tuple(rows)


def digest_fasta(path: Path) -> tuple[str, int, tuple[ReferenceContig, ...]]:
    """Stream ``path`` once; return file SHA-256, size and per-contig digests.

    Per-contig ``md5`` follows the SAM ``M5`` definition: the MD5 of the
    sequence in upper case with all whitespace removed.
    """

    file_digest = hashlib.sha256()
    size = 0
    contigs: list[ReferenceContig] = []
    names: set[str] = set()
    name: str | None = None
    contig_digest = hashlib.md5()
    contig_length = 0
    carry = b""
    first = True

    def finish() -> None:
        if name is None:
            return
        if contig_length == 0:
            raise _fasta_problem("a FASTA record has no sequence")
        contigs.append(
            ReferenceContig(name=name, length=contig_length, md5=contig_digest.hexdigest())
        )

    def sequence(segment: bytes) -> None:
        nonlocal contig_length
        cleaned = segment.translate(_UPPERCASE, _WHITESPACE)
        if not cleaned:
            return
        if name is None:
            raise _fasta_problem("sequence appears before the first FASTA header")
        if b">" in cleaned:
            raise _fasta_problem("FASTA sequence contains a header marker")
        contig_digest.update(cleaned)
        contig_length += len(cleaned)

    def header(line: bytes) -> None:
        nonlocal name, contig_digest, contig_length
        finish()
        try:
            text = line[1:].decode("ascii")
        except UnicodeDecodeError as exc:
            raise _fasta_problem("FASTA header is not ASCII") from exc
        tokens = text.split()
        if not tokens or not _CONTIG_NAME.fullmatch(tokens[0]):
            raise _fasta_problem("FASTA contig name is empty or uses unsupported characters")
        if tokens[0] in names:
            raise _fasta_problem("FASTA contig names are not unique")
        names.add(tokens[0])
        name = tokens[0]
        contig_digest = hashlib.md5()
        contig_length = 0

    def process(block: bytes) -> None:
        # ``block`` always ends at a line boundary, so headers are whole.
        position = 0
        end = len(block)
        while position < end:
            if block.startswith(b">", position):
                line_end = block.find(b"\n", position)
                if line_end < 0:
                    line_end = end
                header(block[position:line_end].rstrip(b"\r"))
                position = line_end + 1
                continue
            next_header = block.find(b"\n>", position)
            if next_header < 0:
                sequence(block[position:])
                return
            sequence(block[position : next_header + 1])
            position = next_header + 1

    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            if first:
                first = False
                if chunk[:2] == b"\x1f\x8b":
                    raise ReferenceProblem(
                        "TBX-REF-001",
                        "FASTA is gzip-compressed",
                        cause="compressed FASTA input is not supported",
                        fix="Decompress the FASTA first, run `samtools faidx`, then register again",
                    )
            file_digest.update(chunk)
            size += len(chunk)
            data = carry + chunk
            cut = data.rfind(b"\n") + 1
            if cut == 0:
                if data.startswith(b">"):
                    carry = data
                else:
                    # One long unwrapped sequence line: no header can start
                    # inside it, so consume it now instead of growing a carry.
                    sequence(data)
                    carry = b""
                continue
            carry = data[cut:]
            process(data[:cut])
    if carry:
        process(carry)
    finish()
    if not contigs:
        raise _fasta_problem("FASTA has no records")
    return file_digest.hexdigest(), size, tuple(contigs)


def _registration_bytes(
    fasta: Path, reference_id: str, assembly: str | None
) -> tuple[RegisteredReference, bytes, bytes]:
    if not fasta.is_file():
        raise ReferenceProblem(
            "TBX-REF-001",
            "FASTA file was not found",
            cause="the --fasta path does not name a regular file",
            fix="Pass the path of an uncompressed FASTA with a `.fai` index",
            exit_code=4,
        )
    with fasta.open("rb") as handle:
        if handle.read(2) == b"\x1f\x8b":
            raise ReferenceProblem(
                "TBX-REF-001",
                "FASTA is gzip-compressed",
                cause="compressed FASTA input is not supported",
                fix="Decompress the FASTA first, run `samtools faidx`, then register again",
            )
    indexed = read_fai(Path(f"{fasta}.fai"))
    sha256, size, contigs = digest_fasta(fasta)
    if tuple((contig.name, contig.length) for contig in contigs) != indexed:
        raise _fai_problem("contig names or lengths in the `.fai` differ from the FASTA")
    try:
        registered = RegisteredReference(
            reference_id=reference_id,
            # The contract requires an assembly label. Without --assembly the
            # label falls back to the reference ID and is never compared.
            assembly=assembly if assembly is not None else reference_id,
            asset_sha256=sha256,
            contigs=contigs,
        )
    except ValidationError as exc:
        raise _fasta_problem("FASTA contigs do not form a valid registered reference") from exc
    source = ReferenceSource(
        fasta_path=str(fasta.resolve()),
        fasta_size_bytes=size,
        assembly_declared=assembly is not None,
    )
    return registered, canonical_json_bytes(registered), canonical_json_bytes(source)


def _write_private(directory_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_JSON_BYTES:
        raise ReferenceProblem(
            "TBX-REF-003",
            "Reference registration is unreadable",
            cause="a registration file is not a regular file of plausible size",
            fix="Remove the damaged registration directory and register again",
        )
    return path.read_bytes()


def register_reference(
    root: Path,
    fasta: Path,
    reference_id: str,
    *,
    assembly: str | None = None,
) -> RegistrationResult:
    """Register ``fasta`` under ``root``; write-once and idempotent.

    The caller holds the workspace mutation lock.
    """

    validate_reference_id(reference_id)
    registered, registration, source = _registration_bytes(Path(fasta), reference_id, assembly)
    parent = references_root(root)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = parent / reference_id
    if destination.exists() or destination.is_symlink():
        return _existing(destination, registered, registration, source)
    staging = Path(tempfile.mkdtemp(prefix=".register-", dir=parent))
    try:
        directory_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            _write_private(directory_fd, REGISTRATION_FILE, registration)
            _write_private(directory_fd, SOURCE_FILE, source)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                rename_directory_exclusive_at(parent_fd, staging.name, reference_id)
            except FileExistsError:
                return _existing(destination, registered, registration, source)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    return RegistrationResult(registered=registered, created=True)


def _existing(
    destination: Path,
    registered: RegisteredReference,
    registration: bytes,
    source: bytes,
) -> RegistrationResult:
    conflict = ReferenceProblem(
        "TBX-REF-002",
        "A different registration already uses this reference ID",
        cause="re-registration bytes differ from the existing write-once registration",
        fix="Keep the existing registration, or register under a new --id",
    )
    if destination.is_symlink() or not destination.is_dir():
        raise conflict
    try:
        same = (
            _read_regular(destination / REGISTRATION_FILE) == registration
            and _read_regular(destination / SOURCE_FILE) == source
        )
    except (OSError, ReferenceProblem) as exc:
        raise conflict from exc
    if not same:
        raise conflict
    return RegistrationResult(registered=registered, created=False)


def _not_registered(reference_id: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-REF-003",
        "Reference not registered under ROOT",
        cause=f"no registration named {reference_id!r} under ROOT/references",
        fix="Run `traceback reference register --fasta PATH --id ID --root ROOT` first",
    )


def load_reference(root: Path, reference_id: str) -> LoadedReference:
    """Load one registration by ID; fail closed on any inconsistency."""

    validate_reference_id(reference_id)
    directory = references_root(root) / reference_id
    if directory.is_symlink() or not directory.is_dir():
        raise _not_registered(reference_id)
    try:
        registered = canonical_model_from_bytes(
            RegisteredReference, _read_regular(directory / REGISTRATION_FILE)
        )
        source = canonical_model_from_bytes(
            ReferenceSource, _read_regular(directory / SOURCE_FILE)
        )
    except FileNotFoundError as exc:
        raise _not_registered(reference_id) from exc
    except (OSError, ValueError) as exc:
        raise ReferenceProblem(
            "TBX-REF-003",
            "Reference registration is unreadable",
            cause="registration files are not canonical registration documents",
            fix="Remove the damaged registration directory and register again",
        ) from exc
    if registered.reference_id != reference_id:
        raise ReferenceProblem(
            "TBX-REF-003",
            "Reference registration is unreadable",
            cause="registration directory name and recorded reference ID differ",
            fix="Remove the damaged registration directory and register again",
        )
    return LoadedReference(registered=registered, source=source)


def list_reference_ids(root: Path) -> tuple[str, ...]:
    """Return registered reference IDs (directory names) in sorted order."""

    parent = references_root(root)
    if not parent.is_dir():
        return ()
    return tuple(
        sorted(
            entry.name
            for entry in parent.iterdir()
            if not entry.name.startswith(".") and REFERENCE_ID_PATTERN.fullmatch(entry.name)
        )
    )


__all__ = [
    "DOCS_ANCHOR",
    "LoadedReference",
    "ReferenceProblem",
    "ReferenceSource",
    "RegistrationResult",
    "digest_fasta",
    "list_reference_ids",
    "load_reference",
    "read_fai",
    "register_reference",
    "validate_reference_id",
]
