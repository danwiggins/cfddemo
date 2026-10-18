"""Bind a registered reference to the FASTA and indexed bases used by Lab.

The persistent proof is an optimization for cooperative local files: a first
use verifies the complete FASTA SHA-256 and each contig's indexed sequence M5.
Subsequent uses compare device/inode/size/mtime/ctime for both the FASTA and
its .fai. Same-UID hostile changes during a tool's read remain outside the
local file contract; callers also check the proof after a consuming step.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

import pysam

from traceback_runner.references import ReferenceProblem, read_fai
from traceback_runner.serialization import canonical_json_bytes

from .file_access import open_regular_no_follow
from .ingest import Reference
from .problems import LabProblem
from .store import LabStore

_BLOCK = 4 * 1024 * 1024
_PROOF_VERSION = "reference-indexed-proof-v1"


@dataclass(frozen=True)
class _Stamp:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _problem(cause: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-004",
        cause=cause,
        fix="Restore the registered FASTA and its matching .fai, or register the correct reference in a new ROOT",
    )


def _stamp(path: Path) -> _Stamp:
    try:
        info = path.lstat()
    except OSError as exc:
        raise _problem("registered reference FASTA or index is missing or unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _problem("registered reference FASTA or index is not a regular file")
    return _Stamp(
        str(path), info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _stamps(fasta: Path) -> tuple[_Stamp, _Stamp]:
    return _stamp(fasta), _stamp(Path(f"{fasta}.fai"))


def _proof_key(reference: Reference, stamps: tuple[_Stamp, _Stamp]) -> str:
    payload = {
        "version": _PROOF_VERSION,
        "registration": reference.loaded.registered.model_dump(mode="json"),
        "fasta": asdict(stamps[0]),
        "fai": asdict(stamps[1]),
    }
    return f"{_PROOF_VERSION}:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _raw_sha256(fasta: Path) -> str:
    digest = hashlib.sha256()
    try:
        with os.fdopen(open_regular_no_follow(fasta), "rb") as stream:
            for block in iter(lambda: stream.read(_BLOCK), b""):
                digest.update(block)
    except OSError as exc:
        raise _problem("registered FASTA cannot be read for SHA-256 verification") from exc
    return digest.hexdigest()


def _check_indexed_contigs(reference: Reference, fasta: Path) -> None:
    expected = reference.loaded.registered.contigs
    indexed = tuple((row.name, row.length) for row in expected)
    try:
        if read_fai(Path(f"{fasta}.fai")) != indexed:
            raise _problem("registered FASTA index names or lengths changed")
        with pysam.FastaFile(str(fasta)) as reader:
            if tuple(zip(reader.references, reader.lengths, strict=True)) != indexed:
                raise _problem("registered FASTA index differs from the reference registration")
            for row in expected:
                digest = hashlib.md5()
                for start in range(0, row.length, _BLOCK):
                    end = min(row.length, start + _BLOCK)
                    bases = reader.fetch(row.name, start, end)
                    if len(bases) != end - start:
                        raise _problem("registered FASTA index returned an incomplete contig")
                    digest.update(bases.upper().encode("ascii"))
                if digest.hexdigest() != row.md5:
                    raise _problem(f"registered FASTA index returns changed bases for {row.name}")
    except LabProblem:
        raise
    except (OSError, ValueError, UnicodeError, ReferenceProblem) as exc:
        raise _problem("registered FASTA index cannot be verified") from exc


def verify_reference_source(
    store: LabStore, reference: Reference, *, write_cache: bool,
) -> None:
    """Verify full registered bytes on first use; then check the strong stamp.

    Dry-run calls with ``write_cache=False`` and never creates or rewrites a
    cache, FASTA or index. Actual calls occur under the Lab writer lock.
    """

    fasta = reference.fasta
    before = _stamps(fasta)
    if before[0].size != reference.loaded.source.fasta_size_bytes:
        raise _problem("registered FASTA size changed since registration")
    key = _proof_key(reference, before)
    cache = store.load_hash_cache()
    proof = cache.get(key)
    if (isinstance(proof, dict) and proof.get("sha256") == reference.sha256
            and proof.get("indexed_m5_verified") is True):
        if _stamps(fasta) != before:
            raise _problem("registered FASTA or index changed during verification")
        return
    if _raw_sha256(fasta) != reference.sha256:
        raise _problem("registered FASTA bytes changed since registration")
    _check_indexed_contigs(reference, fasta)
    if _stamps(fasta) != before:
        raise _problem("registered FASTA or index changed during verification")
    if write_cache:
        cache[key] = {"sha256": reference.sha256, "indexed_m5_verified": True}
        store.save_hash_cache(cache)
