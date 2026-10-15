"""Local reference and method-asset registration (unqualified, local).

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

import bz2
import hashlib
import itertools
import lzma
import os
import re
import stat
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

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
_MAX_HEADER_BYTES = 64 * 1024
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
    except OSError as exc:
        raise _fai_problem("the `.fai` index is not a readable file") from exc
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
                    if len(data) > _MAX_HEADER_BYTES:
                        raise _fasta_problem("a FASTA header line is implausibly long")
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


# ---------------------------------------------------------------------------
# Method assets (signal SH2): ROOT/assets-local/<kind>/<id>/
# ---------------------------------------------------------------------------
#
# A registration records the identity of one local method input file (a
# Loyfer atlas, an ichorCNA wig, ...).  Like a FASTA, the file is referenced,
# never copied at registration: ``registered-asset.json`` holds the kind, ID,
# SHA-256, size and the kind's structural parse check, and ``source.json`` the
# local locator, which must never leave ROOT.  A job copies the file into its
# own sealed directory and re-hashes it there (:func:`copy_registered_asset`).
# Registration does not qualify the asset or any result made with it.

ASSETS_DIRECTORY = "assets-local"
ASSET_REGISTRATION_FILE = "registered-asset.json"
ASSET_SOURCE_FILE = "source.json"
_MAX_TEXT_ASSET_BYTES = 128 * 1024 * 1024
_MAX_PON_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ASSET_LINE_BYTES = 1024 * 1024
# The PoN envelope: decompressed output is read in bounded slices, in total at
# most this much, and xz may use at most this much memory.
_MAX_PON_DECOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_DECOMPRESSION_OUTPUT_BYTES = 1024 * 1024
_XZ_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024


class AssetKind(StrEnum):
    """The closed set of method asset kinds."""

    LOYFER_ATLAS = "loyfer-atlas"
    LOYFER_MARKERS = "loyfer-markers"
    LOYFER_REGIONS = "loyfer-regions"
    ICHOR_GC_WIG = "ichor-gc-wig"
    ICHOR_MAP_WIG = "ichor-map-wig"
    ICHOR_CENTROMERE = "ichor-centromere"
    ICHOR_PON = "ichor-pon"


# ``method-asset register --from-dir``: the Loyfer file each kind is read from
# and the asset ID it is registered under.
LOYFER_DIRECTORY_FILES: dict[AssetKind, tuple[str, str]] = {
    AssetKind.LOYFER_ATLAS: (
        "Atlas.U250.l4.hg38.full.tsv",
        "asset_loyfer_atlas_u250_l4_hg38",
    ),
    AssetKind.LOYFER_MARKERS: ("Markers.U250.hg38.tsv", "asset_loyfer_markers_u250_hg38"),
    AssetKind.LOYFER_REGIONS: (
        "Regions.U250.l4.hg38.bed",
        "asset_loyfer_regions_u250_l4_hg38",
    ),
}

# The file name extension a job copy carries, by kind.
_ASSET_SUFFIX: dict[AssetKind, str] = {
    AssetKind.LOYFER_ATLAS: ".tsv",
    AssetKind.LOYFER_MARKERS: ".tsv",
    AssetKind.LOYFER_REGIONS: ".bed",
    AssetKind.ICHOR_GC_WIG: ".wig",
    AssetKind.ICHOR_MAP_WIG: ".wig",
    AssetKind.ICHOR_CENTROMERE: ".txt",
    AssetKind.ICHOR_PON: ".rds",
}


# The consumer parser each kind is registered through: registration calls the
# same function the analysis calls, so the two cannot disagree on a file.
ASSET_PARSERS: dict[AssetKind, str] = {
    AssetKind.LOYFER_ATLAS: "evidence_inspector.cell_origin_pipeline.read_atlas_u250",
    AssetKind.LOYFER_MARKERS: "evidence_inspector.cell_origin_pipeline.read_marker_metadata",
    AssetKind.LOYFER_REGIONS: "evidence_inspector.cell_origin_pipeline.read_marker_regions",
    AssetKind.ICHOR_GC_WIG: "evidence_inspector.ichor_adapter.parse_fixed_step_wig",
    AssetKind.ICHOR_MAP_WIG: "evidence_inspector.ichor_adapter.parse_fixed_step_wig",
    AssetKind.ICHOR_CENTROMERE: "evidence_inspector.ichor_adapter.parse_centromere_table",
    # Python cannot read an RDS body: only the envelope is checked here and
    # the object itself is validated by readRDS when the analysis runs (CN3).
    AssetKind.ICHOR_PON: "traceback_runner.references.r_serialized_envelope",
}


class AssetParseCheck(RunnerContract):
    """What the kind's consumer parser found; never a scientific validation."""

    schema_version: Literal["traceback.asset-parse-check.v1"] = (
        "traceback.asset-parse-check.v1"
    )
    format: Literal[
        "loyfer-atlas-tsv",
        "loyfer-markers-tsv",
        "bed",
        "wig-fixed-step",
        "ichor-centromere-tsv",
        "r-serialized",
    ]
    parser: str = Field(min_length=1, max_length=128)
    data_rows: int | None = Field(default=None, ge=1)
    columns: int | None = Field(default=None, ge=1)
    bin_size_bp: int | None = Field(default=None, ge=1)


class RegisteredAsset(RunnerContract):
    """Content identity of one registered method asset (no local path)."""

    schema_version: Literal["traceback.registered-asset.v1"] = "traceback.registered-asset.v1"
    kind: AssetKind
    asset_id: str = Field(pattern=REFERENCE_ID_PATTERN.pattern)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1)
    parse_check: AssetParseCheck


class AssetSource(RunnerContract):
    """Local-only locator for a registered asset file; never exported."""

    schema_version: Literal["traceback.asset-source.v1"] = "traceback.asset-source.v1"
    file_path: str = Field(min_length=1, max_length=4096)
    file_size_bytes: int = Field(ge=1)


@dataclass(frozen=True)
class LoadedAsset:
    registered: RegisteredAsset
    source: AssetSource


@dataclass(frozen=True)
class AssetRegistrationResult:
    registered: RegisteredAsset
    created: bool


def assets_root(root: Path) -> Path:
    return Path(root) / ASSETS_DIRECTORY


def _register_command(kind: AssetKind | str | None, asset_id: str) -> str:
    kind_text = kind.value if isinstance(kind, AssetKind) else (kind or "KIND")
    return (
        f"traceback method-asset register --kind {kind_text} --id {asset_id} "
        "--file PATH --root ROOT"
    )


def _asset_conflict(cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-ASSET-001",
        "A different registration already uses this asset ID",
        cause=cause,
        fix="Keep the existing registration, or register the new file under a new --id",
    )


def _asset_changed(asset_id: str, cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-ASSET-002",
        "A registered asset file no longer matches its registration",
        cause=cause,
        fix=(
            f"Restore the original file for {asset_id}, or register the new file "
            "under a new --id (registrations are write-once)"
        ),
    )


def _asset_missing(cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-ASSET-003",
        "An asset file is missing or unreadable",
        cause=cause,
        fix="Restore the file at its registered location (or pass an existing --file)",
        exit_code=4,
    )


def _asset_not_registered(asset_id: str, kind: AssetKind | None, cause: str) -> ReferenceProblem:
    loyfer = kind is None or kind in LOYFER_DIRECTORY_FILES
    fix = f"Run `{_register_command(kind, asset_id)}`"
    if loyfer:
        fix += (
            "; for the three Loyfer files, `traceback method-asset register "
            "--from-dir LOYFER_DIR --root ROOT`"
        )
    if kind in (AssetKind.ICHOR_GC_WIG, AssetKind.ICHOR_MAP_WIG, AssetKind.ICHOR_CENTROMERE):
        fix += (
            "; for the ichorCNA package files, `traceback method-asset register "
            "--from-toolchain copy-number --root ROOT`"
        )
    return ReferenceProblem(
        "TBX-ASSET-004",
        "Asset not registered under ROOT",
        cause=cause,
        fix=fix,
        exit_code=4,
    )


def _asset_malformed(kind: AssetKind, cause: str) -> ReferenceProblem:
    return ReferenceProblem(
        "TBX-ASSET-005",
        f"The file is not a well-formed {kind.value} file; nothing was registered",
        cause=cause,
        fix=f"Pass the unmodified, uncompressed {kind.value} file for --kind {kind.value}",
    )


class _Malformed(ValueError):
    pass


def _parse_with_consumer(kind: AssetKind, path: Path) -> AssetParseCheck:
    """Run the kind's own consumer parser on ``path`` (imported lazily)."""

    parser = ASSET_PARSERS[kind]
    if kind in (AssetKind.LOYFER_ATLAS, AssetKind.LOYFER_MARKERS, AssetKind.LOYFER_REGIONS):
        from evidence_inspector.cell_origin_pipeline import (
            CellOriginPipelineError,
            read_atlas_u250,
            read_marker_metadata,
            read_marker_regions,
        )

        try:
            if kind is AssetKind.LOYFER_ATLAS:
                table = read_atlas_u250(path)
                return AssetParseCheck(
                    format="loyfer-atlas-tsv",
                    parser=parser,
                    data_rows=len(table.rows),
                    columns=8 + len(table.raw_cell_labels),
                )
            if kind is AssetKind.LOYFER_MARKERS:
                rows, _ = read_marker_metadata(path)
                return AssetParseCheck(
                    format="loyfer-markers-tsv",
                    parser=parser,
                    data_rows=len(rows),
                    columns=len(rows[0]),
                )
            regions = read_marker_regions(path)
            return AssetParseCheck(
                format="bed", parser=parser, data_rows=len(regions), columns=3
            )
        except CellOriginPipelineError as exc:
            raise _Malformed(str(exc)) from None
    from evidence_inspector.ichor_adapter import (
        IchorOutputError,
        parse_centromere_table,
        parse_fixed_step_wig,
    )

    try:
        if kind is AssetKind.ICHOR_CENTROMERE:
            intervals = parse_centromere_table(path)
            return AssetParseCheck(
                format="ichor-centromere-tsv",
                parser=parser,
                data_rows=len(intervals),
                columns=4,
            )
        wig = parse_fixed_step_wig(
            path, "gc_wig" if kind is AssetKind.ICHOR_GC_WIG else "map_wig"
        )
        return AssetParseCheck(
            format="wig-fixed-step",
            parser=parser,
            data_rows=len(wig.values),
            bin_size_bp=wig.bin_size_bp,
        )
    except IchorOutputError as exc:
        raise _Malformed(str(exc)) from None


# R serialization (``saveRDS``): the XDR, ASCII or native format header and
# version 2 or 3, raw or inside one complete gzip, bzip2 or xz stream.  This
# is an envelope check only: Python cannot validate the serialized object, so
# ``readRDS`` validates the PoN itself when the copy-number analysis runs.
_R_SERIALIZED_FORMATS = (b"X\n", b"A\n", b"B\n")
_DECOMPRESSION_INPUT_BYTES = 64 * 1024


def _decompressor(head: bytes) -> Any:
    if head.startswith(b"\x1f\x8b"):
        return zlib.decompressobj(wbits=31)
    if head.startswith(b"BZh"):
        return bz2.BZ2Decompressor()
    if head.startswith(b"\xfd7zXZ\x00"):
        return lzma.LZMADecompressor(memlimit=_XZ_MEMORY_LIMIT_BYTES)
    return None


def _check_r_header(head: bytes) -> None:
    if len(head) < 6 or not head.startswith(_R_SERIALIZED_FORMATS):
        raise _Malformed("the file is not an R serialized (.rds) object")
    if head[:2] == b"X\n" and head[2:6] not in (
        b"\x00\x00\x00\x02",
        b"\x00\x00\x00\x03",
    ):
        raise _Malformed("the R serialization version is not 2 or 3")


def _bounded_outputs(decompressor: Any, data: bytes) -> Iterator[bytes]:
    """Feed ``data``; yield decompressed output in slices of bounded size."""

    is_zlib = not hasattr(decompressor, "needs_input")
    output = decompressor.decompress(data, _DECOMPRESSION_OUTPUT_BYTES)
    yield output
    while True:
        if is_zlib:
            if decompressor.eof or not decompressor.unconsumed_tail:
                return
            output = decompressor.decompress(
                decompressor.unconsumed_tail, _DECOMPRESSION_OUTPUT_BYTES
            )
        else:
            if decompressor.eof or decompressor.needs_input:
                return
            output = decompressor.decompress(b"", _DECOMPRESSION_OUTPUT_BYTES)
        yield output


def r_serialized_envelope(chunks: Iterator[bytes]) -> None:
    """Check an ``.rds`` envelope; the object is validated by ``readRDS`` (CN3).

    A compressed file must be one complete gzip, bzip2 or xz stream with
    nothing after it; it is decompressed in bounded slices (at most
    ``_MAX_PON_DECOMPRESSED_BYTES`` in total, xz within a memory limit) and
    only its first six bytes are kept.  Those (or a raw file's) must be an R
    serialization header.
    """

    stream = iter(chunks)
    first = next(stream, b"")
    decompressor = _decompressor(first)
    if decompressor is None:
        _check_r_header(first[:6])
        for _ in stream:
            pass
        return
    head = b""
    total = 0
    for data in itertools.chain((first,), stream):
        for offset in range(0, len(data), _DECOMPRESSION_INPUT_BYTES):
            if decompressor.eof:
                raise _Malformed("the compressed file has data after its stream")
            try:
                for output in _bounded_outputs(
                    decompressor, data[offset : offset + _DECOMPRESSION_INPUT_BYTES]
                ):
                    total += len(output)
                    if total > _MAX_PON_DECOMPRESSED_BYTES:
                        raise _Malformed("the decompressed file is implausibly large")
                    if len(head) < 6:
                        head += output[: 6 - len(head)]
            except (OSError, EOFError, zlib.error, lzma.LZMAError, MemoryError):
                raise _Malformed("the compressed stream is corrupt") from None
            except _Malformed:
                raise
            except ValueError:
                raise _Malformed("the compressed stream is corrupt") from None
    if not decompressor.eof:
        raise _Malformed("the compressed stream is truncated")
    if getattr(decompressor, "unused_data", b"") or getattr(
        decompressor, "unconsumed_tail", b""
    ):
        raise _Malformed("the compressed file has data after its stream")
    _check_r_header(head)


def _scan_asset(kind: AssetKind, path: Path) -> tuple[str, int, AssetParseCheck]:
    """Hash ``path`` once with bounded reads, then run the kind's consumer parser.

    The hashing pass refuses a file over the kind's size cap, a text file with
    any line longer than ``_MAX_ASSET_LINE_BYTES``, and (for the PoN) a bad
    envelope.  The consumer parser then reads the same file.
    """

    digest = hashlib.sha256()
    size = 0
    limit = _MAX_PON_BYTES if kind is AssetKind.ICHOR_PON else _MAX_TEXT_ASSET_BYTES
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise _asset_missing("the file does not exist") from None
    except OSError:
        raise _asset_missing("the file cannot be opened for reading") from None
    with handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _asset_missing("the path is not a regular file")

        def chunks() -> Iterator[bytes]:
            nonlocal size
            line_bytes = 0
            while chunk := handle.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise _Malformed("the file is implausibly large for its kind")
                digest.update(chunk)
                if kind is not AssetKind.ICHOR_PON:
                    # Every line, completed or not, is held to the cap.
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        line_bytes = (line_bytes if index == 0 else 0) + len(part)
                        if line_bytes > _MAX_ASSET_LINE_BYTES:
                            raise _Malformed("a line is implausibly long")
                yield chunk

        try:
            if kind is AssetKind.ICHOR_PON:
                r_serialized_envelope(chunks())
                check = AssetParseCheck(format="r-serialized", parser=ASSET_PARSERS[kind])
            else:
                for _ in chunks():
                    pass
                check = None
        except _Malformed as exc:
            raise _asset_malformed(kind, str(exc)) from None
        except OSError:
            raise _asset_missing("the file could not be read to the end") from None
    if size == 0:
        raise _asset_malformed(kind, "the file is empty")
    if check is None:
        try:
            check = _parse_with_consumer(kind, path)
        except _Malformed as exc:
            raise _asset_malformed(kind, str(exc)) from None
    return digest.hexdigest(), size, check


def _asset_kind(value: AssetKind | str) -> AssetKind:
    try:
        return AssetKind(value)
    except ValueError:
        raise ValueError(
            f"asset kind must be one of {', '.join(item.value for item in AssetKind)}"
        ) from None


def _asset_directories(root: Path, asset_id: str) -> list[tuple[AssetKind, Path]]:
    """Every kind directory that holds ``asset_id`` (registrations or debris)."""

    found = []
    for kind in AssetKind:
        candidate = assets_root(root) / kind.value / asset_id
        if candidate.exists() or candidate.is_symlink():
            found.append((kind, candidate))
    return found


def register_asset(
    root: Path, kind: AssetKind | str, asset_id: str, path: Path
) -> AssetRegistrationResult:
    """Register one method asset file under ``root``; write-once and idempotent.

    The file is hashed and structurally checked for its kind in one read; a
    malformed file raises TBX-ASSET-005 and nothing is written.  The same
    bytes at the same location re-register as a no-op (exit 0); anything else
    under an existing ID raises TBX-ASSET-001.  An ID is unique across kinds.
    The caller holds the workspace mutation lock.
    """

    kind = _asset_kind(kind)
    validate_reference_id(asset_id)
    path = Path(path)
    file_sha256, size, check = _scan_asset(kind, path)
    registered = RegisteredAsset(
        kind=kind,
        asset_id=asset_id,
        file_sha256=file_sha256,
        byte_size=size,
        parse_check=check,
    )
    source = AssetSource(file_path=str(path.resolve()), file_size_bytes=size)
    registration, source_bytes = canonical_json_bytes(registered), canonical_json_bytes(source)
    for other_kind, _ in _asset_directories(root, asset_id):
        if other_kind is not kind:
            raise _asset_conflict(f"the asset ID is already registered as {other_kind.value}")
    parent = assets_root(root) / kind.value
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = parent / asset_id
    if destination.exists() or destination.is_symlink():
        return _existing_asset(destination, registered, registration, source_bytes)
    staging = Path(tempfile.mkdtemp(prefix=".register-", dir=parent))
    try:
        directory_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            _write_private(directory_fd, ASSET_REGISTRATION_FILE, registration)
            _write_private(directory_fd, ASSET_SOURCE_FILE, source_bytes)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                rename_directory_exclusive_at(parent_fd, staging.name, asset_id)
            except FileExistsError:
                return _existing_asset(destination, registered, registration, source_bytes)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    return AssetRegistrationResult(registered=registered, created=True)


def _existing_asset(
    destination: Path,
    registered: RegisteredAsset,
    registration: bytes,
    source: bytes,
) -> AssetRegistrationResult:
    conflict = _asset_conflict(
        "re-registration bytes or location differ from the existing write-once registration"
    )
    if destination.is_symlink() or not destination.is_dir():
        raise conflict
    try:
        same = (
            _read_regular(destination / ASSET_REGISTRATION_FILE) == registration
            and _read_regular(destination / ASSET_SOURCE_FILE) == source
        )
    except (OSError, ReferenceProblem) as exc:
        raise conflict from exc
    if not same:
        raise conflict
    return AssetRegistrationResult(registered=registered, created=False)


def register_loyfer_directory(root: Path, directory: Path) -> tuple[AssetRegistrationResult, ...]:
    """Register the three Loyfer files of ``directory`` under their fixed IDs.

    Every file must be present before any is registered (TBX-ASSET-003 names
    the first missing file name, never a path).  Each registration is
    write-once and idempotent on its own.
    """

    directory = Path(directory)
    for name, _ in LOYFER_DIRECTORY_FILES.values():
        candidate = directory / name
        if not candidate.is_file():
            raise _asset_missing(f"{name} is not in the --from-dir directory")
    return tuple(
        register_asset(root, kind, asset_id, directory / name)
        for kind, (name, asset_id) in LOYFER_DIRECTORY_FILES.items()
    )


# ``method-asset register --from-toolchain copy-number`` (signal CN2): the
# ichorCNA package's own hg38 files, inside the installed toolchain's
# ``lib/R/library/ichorCNA/extdata``.  The names were read from the installed
# r-ichorcna 0.5.1 package; the wigs come in 10, 50, 500 and 1000 kb bins and
# the bin size is the locked method's, never a flag.
ICHOR_EXTDATA_RELPATH = "ichorCNA/extdata"
ICHOR_CENTROMERE_FILE = "GRCh38.GCA_000001405.2_centromere_acen.txt"
ICHOR_TOOLCHAIN_KINDS = (
    AssetKind.ICHOR_GC_WIG,
    AssetKind.ICHOR_MAP_WIG,
    AssetKind.ICHOR_CENTROMERE,
)
_ICHOR_WIG_BIN_SIZES_BP = frozenset({10_000, 50_000, 500_000, 1_000_000})


def ichor_toolchain_files(
    bin_size_bp: int, toolchain_tag: str
) -> dict[AssetKind, tuple[str, str]]:
    """The extdata file each ichorCNA kind is read from, and its asset ID.

    ``toolchain_tag`` (the first 12 hex digits of the toolchain lock SHA-256)
    is part of each ID: a relocked toolchain installs to a new directory, so
    its files register under new IDs instead of colliding with the
    write-once registrations of the old one.
    """

    if bin_size_bp not in _ICHOR_WIG_BIN_SIZES_BP:
        raise ValueError("the ichorCNA package ships no hg38 wig for this bin size")
    if not re.fullmatch(r"[0-9a-f]{12}", toolchain_tag):
        raise ValueError("toolchain tag must be 12 lowercase hex characters")
    kb = f"{bin_size_bp // 1000}kb"
    return {
        AssetKind.ICHOR_GC_WIG: (f"gc_hg38_{kb}.wig", f"asset_ichor_gc_hg38_{kb}_{toolchain_tag}"),
        AssetKind.ICHOR_MAP_WIG: (
            f"map_hg38_{kb}.wig",
            f"asset_ichor_map_hg38_{kb}_{toolchain_tag}",
        ),
        AssetKind.ICHOR_CENTROMERE: (
            ICHOR_CENTROMERE_FILE,
            f"asset_ichor_centromere_grch38_{toolchain_tag}",
        ),
    }


def register_ichor_toolchain_directory(
    root: Path, extdata: Path, *, bin_size_bp: int, toolchain_tag: str
) -> tuple[AssetRegistrationResult, ...]:
    """Register the gc wig, map wig and centromere table of ``extdata``.

    Every file is hashed and parsed first, and each wig's bin size must be
    ``bin_size_bp``; only then is anything registered (each registration is
    write-once and idempotent on its own).  A missing file raises
    TBX-ASSET-003 naming the file, never a path; a wig of another bin size
    raises TBX-ASSET-005.
    """

    extdata = Path(extdata)
    files = ichor_toolchain_files(bin_size_bp, toolchain_tag)
    for kind, (name, _) in files.items():
        candidate = extdata / name
        if not candidate.is_file():
            raise _asset_missing(f"{name} is not in the installed ichorCNA package")
        _, _, check = _scan_asset(kind, candidate)
        if kind is not AssetKind.ICHOR_CENTROMERE and check.bin_size_bp != bin_size_bp:
            raise _asset_malformed(
                kind, f"{name} has {check.bin_size_bp} bp bins; the locked method uses {bin_size_bp}"
            )
    return tuple(
        register_asset(root, kind, asset_id, extdata / name)
        for kind, (name, asset_id) in files.items()
    )


def load_asset(root: Path, asset_id: str, *, kind: AssetKind | str | None = None) -> LoadedAsset:
    """Load one asset registration by ID; fail closed on any inconsistency.

    With ``kind`` the registration must be of that kind.  A missing
    registration raises TBX-ASSET-004 with the exact register command.
    """

    validate_reference_id(asset_id)
    wanted = None if kind is None else _asset_kind(kind)
    found = _asset_directories(root, asset_id)
    if wanted is not None:
        found = [item for item in found if item[0] is wanted]
    if not found:
        raise _asset_not_registered(
            asset_id, wanted, f"no registration named {asset_id!r} under ROOT/{ASSETS_DIRECTORY}"
        )
    found_kind, directory = found[0]
    damaged = ReferenceProblem(
        "TBX-ASSET-004",
        "Asset registration is unreadable",
        cause="the registration files are not canonical asset registration documents",
        fix=(
            f"Remove ROOT/{ASSETS_DIRECTORY}/{found_kind.value}/{asset_id} and run "
            f"`{_register_command(found_kind, asset_id)}`"
        ),
    )
    if len(found) != 1 or directory.is_symlink() or not directory.is_dir():
        raise damaged
    try:
        registered = canonical_model_from_bytes(
            RegisteredAsset, _read_regular(directory / ASSET_REGISTRATION_FILE)
        )
        source = canonical_model_from_bytes(
            AssetSource, _read_regular(directory / ASSET_SOURCE_FILE)
        )
    except (OSError, ValueError, ReferenceProblem) as exc:
        raise damaged from exc
    if (
        registered.asset_id != asset_id
        or registered.kind is not found_kind
        or registered.byte_size != source.file_size_bytes
    ):
        raise damaged
    return LoadedAsset(registered=registered, source=source)


def verify_registered_asset(
    root: Path, asset_id: str, *, kind: AssetKind | str | None = None
) -> LoadedAsset:
    """Re-hash a registered asset at its location (``run``, ``doctor --deep``).

    The read is bounded by the registered size: a longer file is refused as
    soon as it passes that size.  A missing or unreadable file raises
    TBX-ASSET-003; any byte difference raises TBX-ASSET-002.
    """

    loaded = load_asset(root, asset_id, kind=kind)
    registered = loaded.registered
    try:
        with open(loaded.source.file_path, "rb") as handle:
            matches = _bounded_sha256(handle, registered.byte_size) == registered.file_sha256
    except FileNotFoundError:
        raise _asset_missing(f"the registered file for {asset_id} does not exist") from None
    except OSError:
        raise _asset_missing(f"the registered file for {asset_id} cannot be read") from None
    if not matches:
        raise _asset_changed(asset_id, "the file's SHA-256 or size differs from its registration")
    return loaded


def _bounded_sha256(handle: Any, byte_size: int) -> str | None:
    """SHA-256 of exactly ``byte_size`` bytes; ``None`` as soon as there are more
    (or at the end, when there are fewer)."""

    digest = hashlib.sha256()
    remaining = byte_size
    while chunk := handle.read(min(_CHUNK_BYTES, remaining + 1)):
        if len(chunk) > remaining:
            return None
        remaining -= len(chunk)
        digest.update(chunk)
    return digest.hexdigest() if remaining == 0 else None


def asset_copy_name(registered: RegisteredAsset) -> str:
    """The file name a job copy of ``registered`` carries: ``<id><suffix>``."""

    return f"{registered.asset_id}{_ASSET_SUFFIX[registered.kind]}"


def copy_registered_asset(
    root: Path,
    asset_id: str,
    job_directory: Path,
    *,
    kind: AssetKind | str | None = None,
) -> Path:
    """Copy a registered asset into ``job_directory`` and verify it there.

    The copy is written to a private temporary file in ``job_directory``,
    fsynced, published under :func:`asset_copy_name` without replacing
    anything, and then re-hashed from the published file.  A copy whose
    SHA-256 or size differs from the registration is removed and raises
    TBX-ASSET-002; a missing source raises TBX-ASSET-003.  An existing copy
    with the registered digest is reused (a resumed job); one with any other
    digest raises TBX-ASSET-002 and is left in place.  Returns the copy's path.
    """

    loaded = load_asset(root, asset_id, kind=kind)
    registered = loaded.registered
    job_directory = Path(job_directory)
    destination = job_directory / asset_copy_name(registered)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise _asset_changed(asset_id, "the job's copy is not a regular file")
        with destination.open("rb") as existing:
            if _bounded_sha256(existing, registered.byte_size) != registered.file_sha256:
                raise _asset_changed(
                    asset_id, "the job's existing copy differs from the registration"
                )
        return destination
    try:
        source = open(loaded.source.file_path, "rb")  # noqa: SIM115 - closed below
    except FileNotFoundError:
        raise _asset_missing(f"the registered file for {asset_id} does not exist") from None
    except OSError:
        raise _asset_missing(f"the registered file for {asset_id} cannot be read") from None
    temporary = job_directory / f".asset-{registered.asset_id}-{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        with source:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as target:
                remaining = registered.byte_size
                # Never copy more than the registered size: one extra byte
                # refuses the copy before anything is published.
                while chunk := source.read(min(_CHUNK_BYTES, remaining + 1)):
                    if len(chunk) > remaining:
                        raise _asset_changed(
                            asset_id, "the registered file is longer than its registration"
                        )
                    remaining -= len(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        os.link(temporary, destination)  # never replaces an existing name
    finally:
        temporary.unlink(missing_ok=True)
    with destination.open("rb") as published:
        verified = _bounded_sha256(published, registered.byte_size) == registered.file_sha256
    if not verified:
        destination.unlink(missing_ok=True)
        raise _asset_changed(asset_id, "the copy made for this job differs from the registration")
    return destination


def list_assets(root: Path) -> tuple[tuple[AssetKind, str], ...]:
    """Return ``(kind, asset_id)`` of every registration directory, sorted."""

    found = []
    for kind in AssetKind:
        parent = assets_root(root) / kind.value
        if not parent.is_dir():
            continue
        found.extend(
            (kind, entry.name)
            for entry in parent.iterdir()
            if not entry.name.startswith(".") and REFERENCE_ID_PATTERN.fullmatch(entry.name)
        )
    return tuple(sorted(found, key=lambda item: (item[0].value, item[1])))


__all__ = [
    "ASSETS_DIRECTORY",
    "ASSET_PARSERS",
    "DOCS_ANCHOR",
    "ICHOR_CENTROMERE_FILE",
    "ICHOR_EXTDATA_RELPATH",
    "ICHOR_TOOLCHAIN_KINDS",
    "LOYFER_DIRECTORY_FILES",
    "AssetKind",
    "AssetParseCheck",
    "AssetRegistrationResult",
    "AssetSource",
    "LoadedAsset",
    "RegisteredAsset",
    "asset_copy_name",
    "copy_registered_asset",
    "ichor_toolchain_files",
    "list_assets",
    "load_asset",
    "register_asset",
    "r_serialized_envelope",
    "register_ichor_toolchain_directory",
    "register_loyfer_directory",
    "verify_registered_asset",
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
