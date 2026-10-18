"""``lab run`` ingest: chunk safety, identity, disk budget and alignment (D9, D30, D31).

Inputs are one file, several files or a directory of MinKNOW chunks.  Every
path is resolved to an absolute realpath; a symlink, a non-regular file or an
unreadable file is refused (TBX-LAB-005).  No path ever reaches a shell and
every argv element is absolute.

Unaligned chunks (no ``@SQ``) are aligned in one process group::

    samtools cat <chunks in order> | samtools fastq -T MM,ML,MN[,RG] -
      | minimap2 -ax map-ont -y [-R <@RG>] <ref.mmi> -
      | samtools sort -O bam --write-index -o aligned.bam##idx##aligned.bam.bai -

into ``ROOT/lab/inputs/.staging-<random>/``.  The uBAM record total must equal
the aligned primary-plus-unmapped count (TBX-LAB-013) before the directory is
published as ``ROOT/lab/inputs/<input_id>/`` in one rename (D35).
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam

from traceback_runner.references import (
    LoadedReference,
    ReferenceProblem,
    list_reference_ids,
    load_reference,
)
from traceback_runner.serialization import canonical_json_bytes

from .identity import (
    aligned_input_id,
    prealigned_input_id,
    resolve_prefix,
    shortest_unique_prefixes,
    source_id_for_aligned_bam,
    source_id_for_chunks,
)
from .pipeline import Stage, run_pipeline
from .problems import EXIT_USAGE, LabProblem
from .scrub import Scrubber
from .sample_metadata import with_sex_assertion
from .store import LabStore, write_atomic
from .tools import Tool

INPUT_SCHEMA = "traceback.lab.input.v1"
SOURCE_SCHEMA = "traceback.lab.source.v1"
EXAMPLE_REFERENCE_ID = "lab-example"
_CHUNK_INDEX = re.compile(r"_(\d+)\.bam$")
_HASH_BLOCK = 16 * 1024 * 1024
GIB = 1024**3
MARGIN_BYTES = 2 * GIB
# Aligned BAM + sort temp, as multiples of the chunk bytes S (D30).
BUDGET_FACTOR = 2.2
# A minimap2 index is about 2.5x its FASTA (hg38: ~3.1 GB -> ~7.7 GB).
MMI_FACTOR = 2.5
# Measured alignment speed band on an M-series Mac, minutes per GB of uBAM.
ALIGN_MIN_PER_GB = (2.8, 5.5)


# -- input paths ---------------------------------------------------------------
def _refuse_path(position: int, kind: str, shown: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-005",
        cause=f"argument {position} ({shown}) is {kind}",
        fix="Pass the real file (realpath) or fix its permissions",
        data={"argument": position, "kind": kind},
    )


def _check_regular(path: Path, position: int, shown: str) -> Path:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise _refuse_path(position, "missing", shown) from exc
    except OSError as exc:
        raise _refuse_path(position, "unreadable", shown) from exc
    if stat.S_ISLNK(info.st_mode):
        raise _refuse_path(position, "a symlink", shown)
    if not stat.S_ISREG(info.st_mode):
        raise _refuse_path(position, "not a regular file", shown)
    if not os.access(path, os.R_OK):
        raise _refuse_path(position, "unreadable", shown)
    return Path(os.path.realpath(path))


def collect_paths(arguments: Sequence[str | Path]) -> list[Path]:
    """Expand arguments to absolute realpaths of regular ``.bam`` files."""

    found: list[Path] = []
    for position, argument in enumerate(arguments, start=1):
        path = Path(os.path.abspath(Path(argument).expanduser()))
        shown = Path(argument).name or str(argument)
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise _refuse_path(position, "missing", shown) from exc
        if stat.S_ISLNK(info.st_mode):
            raise _refuse_path(position, "a symlink", shown)
        if stat.S_ISDIR(info.st_mode):
            entries = sorted(
                entry for entry in os.listdir(path) if entry.endswith(".bam") and not entry.startswith(".")
            )
            if not entries:
                raise _refuse_path(position, "a directory with no .bam files", shown)
            for entry in entries:
                found.append(_check_regular(path / entry, position, entry))
            continue
        found.append(_check_regular(path, position, shown))
    return found


def chunk_index(name: str) -> int | None:
    match = _CHUNK_INDEX.search(name)
    return int(match.group(1)) if match else None


def _order_key(path: Path) -> tuple[int, int, str]:
    index = chunk_index(path.name)
    return (0 if index is not None else 1, index if index is not None else 0, path.name)


# -- chunk inspection ------------------------------------------------------------
@dataclass
class Chunk:
    path: Path
    name: str
    index: int | None
    size: int
    mtime_ns: int = 0
    sha256: str = ""
    records: int = 0
    rg_lines: tuple[str, ...] = ()
    basecall_model: str | None = None
    aligned: bool = False
    sq: tuple[tuple[str, int], ...] = ()

    @property
    def stem(self) -> str:
        return self.path.name[: -len(".bam")] if self.path.name.endswith(".bam") else self.path.name


@dataclass
class ChunkSet:
    chunks: list[Chunk]
    excluded: list[Chunk] = field(default_factory=list)

    @property
    def aligned(self) -> bool:
        return self.chunks[0].aligned

    @property
    def total_bytes(self) -> int:
        return sum(chunk.size for chunk in self.chunks)

    @property
    def total_records(self) -> int:
        return sum(chunk.records for chunk in self.chunks)

    @property
    def excluded_indexes(self) -> list[int]:
        return sorted(
            chunk.index if chunk.index is not None else -1 for chunk in self.excluded
        )

    @property
    def basecall_model(self) -> str | None:
        return self.chunks[0].basecall_model

    @property
    def rg_lines(self) -> list[str]:
        lines: list[str] = []
        for chunk in self.chunks:
            for line in chunk.rg_lines:
                if line not in lines:
                    lines.append(line)
        return lines

    @property
    def stems(self) -> list[str]:
        return [chunk.stem for chunk in [*self.chunks, *self.excluded]]

    @property
    def source_id(self) -> str:
        if self.aligned:
            return source_id_for_aligned_bam(self.chunks[0].sha256)
        return source_id_for_chunks(
            [chunk.sha256 for chunk in self.chunks], excluded=self.excluded_indexes
        )


def _quickcheck(path: Path) -> bool:
    try:
        pysam.quickcheck("-u", str(path))
    except Exception:  # noqa: BLE001 - pysam raises SamtoolsError (and others) on failure
        return False
    return True


def _rg_model(line: str) -> str | None:
    match = re.search(r"basecall_model=([^\s\t]+)", line)
    return match.group(1) if match else None


def _read_header(chunk: Chunk) -> None:
    with pysam.AlignmentFile(str(chunk.path), "rb", check_sq=False) as bam:
        header = bam.header
        text = str(header)
        chunk.rg_lines = tuple(line for line in text.splitlines() if line.startswith("@RG\t"))
        chunk.sq = tuple((name, int(length)) for name, length in zip(header.references, header.lengths))
        chunk.aligned = bool(chunk.sq)
    models = sorted({model for line in chunk.rg_lines if (model := _rg_model(line))})
    chunk.basecall_model = "|".join(models) if models else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def count_records(path: Path, *flags: str) -> int:
    """``samtools view -c`` through pysam (no external samtools needed)."""

    output = pysam.view("-c", *flags, str(path))
    return int(str(output).strip())


def inspect_chunks(
    paths: Sequence[Path],
    store: LabStore,
    *,
    skip_bad_chunks: bool = False,
    write_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ChunkSet:
    """Quickcheck, order, hash (cached), count (cached) and cross-check chunks."""

    ordered = sorted(paths, key=_order_key)
    chunks: list[Chunk] = []
    bad: list[Chunk] = []
    for path in ordered:
        info = os.stat(path)
        chunk = Chunk(
            path=path,
            name=path.name,
            index=chunk_index(path.name),
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
        )
        (chunks if _quickcheck(path) else bad).append(chunk)
    if bad and not skip_bad_chunks:
        indexes = [chunk.index for chunk in bad]
        raise LabProblem(
            "TBX-LAB-006",
            cause=f"{len(bad)} chunk(s) fail `samtools quickcheck -u` (truncated): indexes {indexes}",
            fix="Re-download those chunks, or rerun with --skip-bad-chunks (results marked provisional)",
            data={"chunk_indexes": indexes},
        )
    if not chunks:
        raise LabProblem(
            "TBX-LAB-006",
            cause="no chunk passes `samtools quickcheck -u`",
            fix="Re-download the chunks",
            data={"chunk_indexes": [chunk.index for chunk in bad]},
        )
    cache = store.load_hash_cache()
    changed = False
    for number, chunk in enumerate(chunks, start=1):
        key = store.hash_cache_key(chunk.path)
        cached = cache.get(key)
        if cached and isinstance(cached.get("sha256"), str) and isinstance(cached.get("records"), int):
            chunk.sha256 = cached["sha256"]
            chunk.records = cached["records"]
        else:
            if progress is not None:
                progress(f"ingest  hashing chunk {number}/{len(chunks)}")
            chunk.sha256 = _sha256_file(chunk.path)
            chunk.records = count_records(chunk.path)
            cache[key] = {"sha256": chunk.sha256, "records": chunk.records}
            changed = True
        _read_header(chunk)
    if changed and write_cache:
        store.save_hash_cache(cache)
    _refuse_duplicates(chunks)
    _refuse_mixed_models(chunks)
    aligned = {chunk.aligned for chunk in chunks}
    if len(aligned) > 1 or (True in aligned and len(chunks) > 1):
        raise LabProblem(
            "TBX-LAB-009",
            cause="inputs mix aligned and unaligned BAMs, or name several aligned BAMs",
            fix="Pass the unaligned chunks and let the Lab align, or one aligned BAM",
            data={"aligned": sum(chunk.aligned for chunk in chunks), "unaligned": sum(not chunk.aligned for chunk in chunks)},
        )
    return ChunkSet(chunks=chunks, excluded=bad)


def require_unchanged(chunkset: ChunkSet) -> None:
    """Refuse when a chunk changed after it was hashed (size or mtime moved).

    The hash and the alignment read the same path at different times; this
    binds the published identity to the bytes the pipeline read, as far as
    the filesystem's metadata can tell.
    """

    for chunk in chunkset.chunks:
        try:
            info = os.stat(chunk.path)
        except OSError as exc:
            raise _refuse_path(0, "gone during the run", chunk.name) from exc
        if (info.st_size, info.st_mtime_ns) != (chunk.size, chunk.mtime_ns):
            raise LabProblem(
                "TBX-LAB-005",
                cause=f"chunk {chunk.index} changed while the run was reading it",
                fix="Rerun once the files are no longer being written or replaced",
                data={"argument": 0, "kind": "changed during the run", "chunk_index": chunk.index},
            )


def _refuse_duplicates(chunks: Sequence[Chunk]) -> None:
    by_hash: dict[str, Chunk] = {}
    by_index: dict[int, Chunk] = {}
    for chunk in chunks:
        if chunk.sha256 in by_hash:
            other = by_hash[chunk.sha256]
            raise LabProblem(
                "TBX-LAB-007",
                cause=f"chunks {other.index} and {chunk.index} have identical content",
                fix="Remove the duplicate download",
                data={"chunk_indexes": [other.index, chunk.index], "kind": "same_content"},
            )
        by_hash[chunk.sha256] = chunk
        if chunk.index is not None:
            if chunk.index in by_index:
                raise LabProblem(
                    "TBX-LAB-007",
                    cause=f"chunk index {chunk.index} appears twice with different content",
                    fix="Remove the duplicate download",
                    data={"chunk_indexes": [chunk.index, chunk.index], "kind": "same_index"},
                )
            by_index[chunk.index] = chunk


def _refuse_mixed_models(chunks: Sequence[Chunk]) -> None:
    for chunk in chunks:
        if chunk.basecall_model and "|" in chunk.basecall_model:
            raise LabProblem(
                "TBX-LAB-008",
                cause=f"chunk {chunk.index} declares several @RG basecall models: "
                + chunk.basecall_model.replace("|", ", "),
                fix="Run each model's chunks as a separate sample",
                data={"models": {model: 1 for model in chunk.basecall_model.split("|")}},
            )
    counts: dict[str, int] = {}
    for chunk in chunks:
        key = chunk.basecall_model or "(none declared)"
        counts[key] = counts.get(key, 0) + 1
    if len(counts) > 1:
        raise LabProblem(
            "TBX-LAB-008",
            cause="chunks declare different @RG basecall models: "
            + ", ".join(f"{model} x{count}" for model, count in sorted(counts.items())),
            fix="Run each model's chunks as a separate sample",
            data={"models": counts},
        )


# -- reference -----------------------------------------------------------------------
@dataclass(frozen=True)
class Reference:
    reference_id: str
    loaded: LoadedReference

    @property
    def sha256(self) -> str:
        return self.loaded.registered.asset_sha256

    @property
    def fasta(self) -> Path:
        return Path(self.loaded.source.fasta_path)

    @property
    def contigs(self) -> tuple[tuple[str, int], ...]:
        return tuple((contig.name, contig.length) for contig in self.loaded.registered.contigs)


def resolve_reference(root: Path, requested: str | None, *, example: bool = False) -> Reference:
    """``--reference ID``, else the only registered reference (TBX-LAB-004).

    The example's ``lab-example`` reference is never a default for real input.
    """

    if requested is None:
        ids = [
            reference_id
            for reference_id in list_reference_ids(root)
            if example or reference_id != EXAMPLE_REFERENCE_ID
        ]
        if not ids:
            raise LabProblem(
                "TBX-LAB-004",
                cause="no reference is registered under ROOT",
                fix="`traceback reference register --fasta hg38.fa --id hg38 --root R`",
                data={"registered": []},
            )
        if len(ids) > 1:
            raise LabProblem(
                "TBX-LAB-004",
                cause="several references are registered: " + ", ".join(ids),
                fix="Add --reference ID",
                data={"registered": ids},
                exit_code=EXIT_USAGE,
            )
        requested = ids[0]
    try:
        loaded = load_reference(root, requested)
    except (ReferenceProblem, ValueError) as exc:
        raise LabProblem(
            "TBX-LAB-004",
            cause=f"reference {requested!r} is not registered or unreadable",
            fix="`traceback reference register --fasta hg38.fa --id hg38 --root R`, or add --reference ID",
            data={"registered": list(list_reference_ids(root))},
        ) from exc
    return Reference(reference_id=requested, loaded=loaded)


def check_sq(chunk: Chunk, reference: Reference) -> None:
    """An aligned input's @SQ must equal the registered reference (TBX-LAB-009)."""

    registered = dict(reference.contigs)
    present = dict(chunk.sq)
    differences: list[str] = []
    for name, length in chunk.sq:
        if name not in registered:
            differences.append(f"{name}: not in reference")
        elif registered[name] != length:
            differences.append(f"{name}: length {length} vs {registered[name]}")
    for name in registered:
        if name not in present:
            differences.append(f"{name}: missing from BAM")
    if differences:
        raise LabProblem(
            "TBX-LAB-009",
            cause=f"@SQ differs from {reference.reference_id} in {len(differences)} contig(s): "
            + "; ".join(differences[:3]),
            fix="Pass the unaligned chunks and let the Lab align, or register the FASTA it was aligned to",
            data={"differences": differences[:3], "total": len(differences)},
        )


# -- disk budget (D30) -----------------------------------------------------------------
def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


@dataclass(frozen=True)
class Budget:
    parts: dict[str, int]
    free: int

    @property
    def required(self) -> int:
        return sum(self.parts.values())

    @property
    def ok(self) -> bool:
        return self.free >= self.required

    def public(self) -> dict[str, Any]:
        return {"required_bytes": self.required, "free_bytes": self.free, "parts": dict(self.parts)}


def disk_budget(store: LabStore, chunkset: ChunkSet, *, mmi_needed: bool, fasta_bytes: int) -> Budget:
    size = chunkset.total_bytes
    if chunkset.aligned:
        parts = {"aligned": int(1.1 * size)}
    else:
        parts = {"aligned": int(1.2 * size), "sort_temp": int((BUDGET_FACTOR - 1.2) * size)}
        if mmi_needed:
            parts["mmi"] = int(MMI_FACTOR * fasta_bytes)
    parts["margin"] = MARGIN_BYTES
    free = shutil.disk_usage(_existing_ancestor(store.root)).free
    return Budget(parts=parts, free=free)


def require_budget(budget: Budget, step: str) -> None:
    if budget.ok:
        return
    need = budget.required / GIB
    have = budget.free / GIB
    raise LabProblem(
        "TBX-LAB-010",
        cause=(
            f"{step} needs {budget.required:,} bytes ({need:.1f} GiB) on ROOT's volume; "
            f"{budget.free:,} bytes ({have:.1f} GiB) are free"
        ),
        fix="Free space, or use --root on a larger volume; --dry-run shows the budget",
        data={"step": step, "volume": "ROOT", **budget.public()},
    )


def estimate_align_minutes(chunk_bytes: int) -> tuple[float, float]:
    gb = chunk_bytes / 1e9
    return (gb * ALIGN_MIN_PER_GB[0], gb * ALIGN_MIN_PER_GB[1])


# -- minimap2 index -----------------------------------------------------------------------
def mmi_path(store: LabStore, reference: Reference) -> Path:
    return store.lab / "refs" / f"{reference.sha256}.mmi"


def cached_sha256(store: LabStore, path: Path) -> str:
    cache = store.load_hash_cache()
    key = store.strong_hash_cache_key(path)
    if key in cache and isinstance(cache[key].get("sha256"), str):
        if store.strong_hash_cache_key(path) == key:
            return cache[key]["sha256"]
    digest = _sha256_file(path)
    if store.strong_hash_cache_key(path) != key:
        raise LabProblem(
            "TBX-LAB-004",
            cause="minimap2 index changed while it was being hashed",
            fix="Restore the unchanged index or rebuild it from the registered reference",
        )
    cache[key] = {"sha256": digest, "records": -1}
    store.save_hash_cache(cache)
    return digest


def ensure_mmi(
    store: LabStore,
    reference: Reference,
    minimap2: Tool,
    *,
    reference_proof: str,
    override: Path | None,
    scrubber: Scrubber,
    timeout_s: float,
    echo: Callable[[str], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, str]:
    """The minimap2 index: ``--mmi`` as given, else built once per reference."""

    if override is not None:
        path = _check_regular(Path(os.path.abspath(override)), 0, override.name)
        return path, cached_sha256(store, path)
    final = mmi_path(store, reference)
    if final.is_file():
        return final, cached_sha256(store, final)
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(f".staging-{os.urandom(6).hex()}.mmi")
    if progress is not None:
        progress("align   building the minimap2 index once (hg38: ~20-40 min, ~8 GB)")
    from .reference_integrity import verify_reference_source

    try:
        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
        result = run_pipeline(
            [Stage("minimap2-index", [str(minimap2.path), "-x", "map-ont", "-d", str(staging), str(reference.fasta)])],
            timeout_s=timeout_s,
            echo=echo,
        )
        if not result.ok:
            raise _align_failed(result, scrubber, "minimap2 index build")
        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
        os.replace(staging, final)
    finally:
        staging.unlink(missing_ok=True)
    return final, cached_sha256(store, final)


def _no_space(stderr_lines: Sequence[str]) -> bool:
    return any("No space left on device" in line for line in stderr_lines)


def _align_failed(result: Any, scrubber: Scrubber, what: str) -> LabProblem:
    stage = result.failed_stage
    lines = [line for outcome in result.stages for line in outcome.stderr.raw_lines()]
    if _no_space(lines):
        return LabProblem(
            "TBX-LAB-011",
            cause=f"the volume filled up during {what}; staging removed",
            fix="Free space, or use --root on a larger volume; then rerun",
            data={"step": "align", "volume": "ROOT"},
        )
    detail = "timed out" if result.timed_out else result.first_line(scrubber)
    return LabProblem(
        "TBX-LAB-014",
        cause=f"{what} failed: {detail}",
        fix="Rerun with --verbose to see live stderr; check `traceback lab doctor`",
        data={
            "process": stage.name if stage is not None else None,
            "exit_code": stage.returncode if stage is not None else None,
            "timed_out": result.timed_out,
            "first_line": detail,
        },
    )


# -- alignment ---------------------------------------------------------------------------------
FASTQ_TAGS = "MM,ML,MN"
ALIGNER_IDENTITY_ARGV = (
    "samtools cat",
    f"samtools fastq -T {FASTQ_TAGS}",
    "minimap2 -ax map-ont -y",
    "samtools sort -O bam --write-index",
)


def aligner_argv_identity(rg_lines: Sequence[str]) -> list[str]:
    """The path- and thread-free argv that enters ``input_id`` (D31)."""

    argv = list(ALIGNER_IDENTITY_ARGV)
    if len(rg_lines) == 1:
        argv[2] = "minimap2 -ax map-ont -y -R <@RG>"
    elif len(rg_lines) > 1:
        argv[1] = f"samtools fastq -T {FASTQ_TAGS},RG"
        argv.insert(3, f"samtools addreplacerg -m orphan_only x{len(rg_lines)}")
    return argv


@dataclass(frozen=True)
class AlignedInput:
    input_id: str
    source_id: str
    directory: Path
    manifest: dict[str, Any]
    reused: bool

    @property
    def bam(self) -> Path:
        return self.directory / "aligned.bam"


def input_manifest(store: LabStore, input_id: str) -> dict[str, Any] | None:
    import json

    try:
        return json.loads((store.inputs_dir / input_id / "manifest.json").read_bytes())
    except (FileNotFoundError, ValueError):
        return None


def _threads(requested: int | None) -> int:
    if requested is not None:
        return max(1, requested)
    return max(1, min(8, (os.cpu_count() or 4) - 2))


def alignment_stages(
    chunkset: ChunkSet,
    *,
    samtools: Tool,
    minimap2: Tool,
    mmi: Path,
    staging: Path,
    threads: int,
    sort_mem: str,
) -> list[Stage]:
    rg = chunkset.rg_lines
    tags = FASTQ_TAGS if len(rg) <= 1 else f"{FASTQ_TAGS},RG"
    sam = str(samtools.path)
    mm2 = [str(minimap2.path), "-ax", "map-ont", "-y", "-t", str(threads)]
    if len(rg) == 1:
        mm2 += ["-R", rg[0].replace("\t", "\\t")]
    mm2 += [str(mmi), "-"]
    stages = [
        Stage("samtools cat", [sam, "cat", *(str(chunk.path) for chunk in chunkset.chunks)]),
        Stage("samtools fastq", [sam, "fastq", "-T", tags, "-"]),
        Stage("minimap2", mm2),
    ]
    if len(rg) > 1:
        for number, line in enumerate(rg):
            stages.append(
                Stage(
                    f"samtools addreplacerg {number}",
                    [sam, "addreplacerg", "-m", "orphan_only", "-u", "--no-PG", "-r", line, "-"],
                )
            )
    bam = staging / "aligned.bam"
    stages.append(
        Stage(
            "samtools sort",
            [
                sam,
                "sort",
                f"-@{max(1, threads // 2)}",
                "-m",
                sort_mem,
                "-O",
                "bam",
                "-T",
                str(staging / "sort"),
                "--write-index",
                "-o",
                f"{bam}##idx##{bam}.bai",
                "-",
            ],
        )
    )
    return stages


def align(
    store: LabStore,
    chunkset: ChunkSet,
    reference: Reference,
    *,
    samtools: Tool,
    minimap2: Tool,
    mmi: Path,
    mmi_sha256: str,
    reference_proof: str,
    threads: int | None,
    sort_mem: str,
    timeout_s: float,
    scrubber: Scrubber,
    echo: Callable[[str], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> AlignedInput:
    """Align into staging, reconcile counts, publish ``inputs/<input_id>/``."""

    source_id = chunkset.source_id
    argv_identity = aligner_argv_identity(chunkset.rg_lines)
    input_id = aligned_input_id(
        source_id=source_id,
        reference_sha256=reference.sha256,
        mmi_sha256=mmi_sha256,
        minimap2_version=str(minimap2.version),
        samtools_version=str(samtools.version),
        aligner_argv=argv_identity,
    )
    existing = input_manifest(store, input_id)
    if existing is not None:
        return AlignedInput(input_id, source_id, store.inputs_dir / input_id, existing, reused=True)
    count = threads_used = _threads(threads)
    started = time.monotonic()
    metrics: dict[str, Any] = {}

    def tick(elapsed: float) -> None:
        second = int(elapsed)
        if progress is not None and second and second % 30 == 0 and second not in metrics.setdefault("ticks", set()):
            metrics["ticks"].add(second)
            low, high = estimate_align_minutes(chunkset.total_bytes)
            progress(f"align   {int(elapsed // 60)}m{int(elapsed % 60):02d}s elapsed (estimate {low:.0f}-{high:.0f} min)")

    def build(staging: Path) -> None:
        from .reference_integrity import verify_reference_source

        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
        mmi_proof = store.strong_hash_cache_key(mmi)
        stages = alignment_stages(
            chunkset,
            samtools=samtools,
            minimap2=minimap2,
            mmi=mmi,
            staging=staging,
            threads=count,
            sort_mem=sort_mem,
        )
        try:
            result = run_pipeline(
                stages, timeout_s=timeout_s, echo=echo, disk_path=staging, on_tick=tick, cwd=staging
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise LabProblem(
                    "TBX-LAB-011",
                    cause="the volume filled up during alignment; staging removed",
                    fix="Free space, or use --root on a larger volume; then rerun",
                    data={"step": "align", "volume": "ROOT"},
                ) from exc
            raise
        if not result.ok:
            raise _align_failed(result, scrubber, "alignment")
        metrics["duration_s"] = round(result.duration_s, 1)
        metrics["peak_rss_bytes"] = result.peak_rss_bytes
        metrics["peak_disk_bytes"] = result.peak_disk_bytes
        metrics["stage_rss_bytes"] = {stage.name: stage.peak_rss_bytes for stage in result.stages}
        bam = staging / "aligned.bam"
        require_unchanged(chunkset)
        staged = Chunk(path=bam, name=bam.name, index=None, size=0)
        _read_header(staged)
        check_sq(staged, reference)  # e.g. --mmi built from another reference
        aligned_count = count_records(bam, "-F", "0x900")
        if aligned_count != chunkset.total_records:
            raise LabProblem(
                "TBX-LAB-013",
                cause=(
                    f"uBAM records {chunkset.total_records:,} != aligned primary + unmapped "
                    f"records {aligned_count:,}"
                ),
                fix="Not retryable as-is: `traceback lab runs RUN_ID --step align`, then report",
                data={"ubam_records": chunkset.total_records, "aligned_records": aligned_count},
            )
        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
        if store.strong_hash_cache_key(mmi) != mmi_proof:
            raise LabProblem(
                "TBX-LAB-004",
                cause="minimap2 index changed during alignment",
                fix="Restore the unchanged index or rebuild it from the registered reference",
            )
        if cached_sha256(store, mmi) != mmi_sha256:
            raise LabProblem(
                "TBX-LAB-004",
                cause="minimap2 index bytes changed during alignment",
                fix="Restore the unchanged index or rebuild it from the registered reference",
            )
        for leftover in staging.glob("sort*.bam"):
            leftover.unlink()
        manifest = {
            "schema": INPUT_SCHEMA,
            "input_id": input_id,
            "source_id": source_id,
            "kind": "aligned",
            "platform": "ont",
            "reference_id": reference.reference_id,
            "reference_sha256": reference.sha256,
            "mmi_sha256": mmi_sha256,
            "tools": {"samtools": samtools.version, "minimap2": minimap2.version},
            "aligner_argv": argv_identity,
            "basecall_model": chunkset.basecall_model,
            "chunks": len(chunkset.chunks),
            "excluded_chunks": chunkset.excluded_indexes,
            "records": {"ubam": chunkset.total_records, "aligned_primary_plus_unmapped": aligned_count},
            "bam_bytes": bam.stat().st_size + (staging / "aligned.bam.bai").stat().st_size,
            "align": {
                "threads": threads_used,
                "duration_s": metrics["duration_s"],
                "peak_rss_bytes": metrics["peak_rss_bytes"],
                "peak_disk_bytes": metrics["peak_disk_bytes"],
                "stage_rss_bytes": metrics["stage_rss_bytes"],
            },
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        metrics["manifest"] = manifest

    store.publish_directory(store.inputs_dir, input_id, build)
    store.bump_generation()
    metrics.setdefault("duration_s", round(time.monotonic() - started, 1))
    return AlignedInput(input_id, source_id, store.inputs_dir / input_id, metrics["manifest"], reused=False)


def ingest_prealigned(
    store: LabStore,
    chunkset: ChunkSet,
    reference: Reference,
    *,
    reference_proof: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> AlignedInput:
    """Copy one already-aligned BAM into the store and index it."""

    if len(chunkset.chunks) != 1:
        raise LabProblem(
            "TBX-LAB-009", cause="prealigned input must contain exactly one BAM",
            fix="Pass one aligned BAM, or unaligned chunks for Lab alignment",
        )
    chunk = chunkset.chunks[0]
    check_sq(chunk, reference)
    source_id = chunkset.source_id
    from .platforms import from_rg_lines

    platform = from_rg_lines(chunkset.rg_lines)
    if platform == "unknown":
        raise LabProblem(
            "TBX-LAB-024", cause="prealigned BAM has no unanimous supported platform declaration",
            fix="Use a BAM whose read groups all declare PL:ONT or PL:ILLUMINA",
        )
    from .reference_integrity import verify_reference_source

    if reference_proof is None:
        reference_proof = verify_reference_source(store, reference, write_cache=True)
    else:
        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
    input_id = prealigned_input_id(source_id=source_id, reference_sha256=reference.sha256, platform=platform)
    existing = input_manifest(store, input_id)
    if existing is not None:
        return AlignedInput(input_id, source_id, store.inputs_dir / input_id, existing, reused=True)
    started = time.monotonic()
    holder: dict[str, Any] = {}

    def build(staging: Path) -> None:
        if progress is not None:
            progress("ingest  copying the aligned BAM into the store")
        bam = staging / "aligned.bam"
        shutil.copyfile(chunk.path, bam)
        if _sha256_file(bam) != chunk.sha256:
            raise LabProblem(
                "TBX-LAB-005",
                cause="the aligned BAM changed while it was copied",
                fix="Rerun once the file is no longer being written",
                data={"argument": 1, "kind": "changing"},
            )
        pysam.index(str(bam), str(staging / "aligned.bam.bai"))
        verify_reference_source(store, reference, write_cache=True, expected_proof=reference_proof)
        manifest = {
            "schema": INPUT_SCHEMA,
            "input_id": input_id,
            "source_id": source_id,
            "kind": "prealigned",
            "platform": platform,
            "reference_id": reference.reference_id,
            "reference_sha256": reference.sha256,
            "basecall_model": chunkset.basecall_model,
            "chunks": 1,
            "excluded_chunks": [],
            "records": {"ubam": None, "aligned_primary_plus_unmapped": count_records(bam, "-F", "0x900")},
            "bam_bytes": bam.stat().st_size + (staging / "aligned.bam.bai").stat().st_size,
            "align": {"duration_s": round(time.monotonic() - started, 1)},
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        holder["manifest"] = manifest

    store.publish_directory(store.inputs_dir, input_id, build)
    store.bump_generation()
    return AlignedInput(input_id, source_id, store.inputs_dir / input_id, holder["manifest"], reused=False)


# -- sources (D11) -----------------------------------------------------------------------------
def source_record(store: LabStore, source_id: str) -> dict[str, Any] | None:
    import json

    try:
        return json.loads((store.sources_dir / f"{source_id}.json").read_bytes())
    except (FileNotFoundError, ValueError):
        return None


def write_source(
    store: LabStore,
    chunkset: ChunkSet,
    aligned: AlignedInput,
    *,
    example: bool = False,
    declared_sex: str | None = None,
) -> dict[str, Any]:
    """Hashes only, never paths: chunk list, exclusions and inputs by aligner."""

    from .platforms import from_rg_lines

    stored = source_record(store, aligned.source_id) or {}
    previous = with_sex_assertion(stored, declared_sex)
    assertion_added = previous is not stored
    platform = aligned.manifest.get("platform")
    if platform is None:
        platform = from_rg_lines(chunkset.rg_lines) if aligned.manifest.get("kind") == "prealigned" else "ont"
    inputs = [entry for entry in previous.get("inputs", []) if entry.get("input_id") != aligned.input_id]
    inputs.append(
        {
            "input_id": aligned.input_id,
            "kind": aligned.manifest.get("kind"),
            "platform": platform,
            "tools": aligned.manifest.get("tools"),
            "reference_id": aligned.manifest.get("reference_id"),
            "used_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    record = {
        "schema": SOURCE_SCHEMA,
        "source_id": aligned.source_id,
        "example": bool(example or previous.get("example", False)),
        "chunks": [
            {"index": chunk.index, "sha256": chunk.sha256, "size": chunk.size, "records": chunk.records}
            for chunk in chunkset.chunks
        ],
        "excluded": chunkset.excluded_indexes,
        "basecall_model": chunkset.basecall_model,
        "created_utc": previous.get("created_utc") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": inputs,
        "derived": previous.get("derived", []),
    }
    if "operator_metadata" in previous:
        record["operator_metadata"] = previous["operator_metadata"]
    if "last_completed_run" in previous:
        record["last_completed_run"] = previous["last_completed_run"]
    write_atomic(store.sources_dir / f"{aligned.source_id}.json", canonical_json_bytes(record))
    if assertion_added:
        store.bump_generation()
    return record


def record_source_sex(store: LabStore, source_id: str, declared_sex: str | None) -> dict[str, Any]:
    """Attach a new assertion to a stored sample under the caller's writer lock."""

    record = source_record(store, source_id)
    if record is None:
        raise LabProblem(
            "TBX-LAB-017", cause="stored sample record is missing",
            fix="Rerun with the chunk paths to restore the sample record",
        )
    updated = with_sex_assertion(record, declared_sex)
    if updated is not record:
        write_atomic(store.sources_dir / f"{source_id}.json", canonical_json_bytes(updated))
        store.bump_generation()
    return updated


def source_ids(store: LabStore) -> list[str]:
    if not store.sources_dir.is_dir():
        return []
    return sorted(
        path.stem
        for path in store.sources_dir.glob("*.json")
        if re.fullmatch(r"[0-9a-f]{64}", path.stem)
    )


def id12_for(store: LabStore, source_id: str) -> str:
    ids = set(source_ids(store)) | {source_id}
    return shortest_unique_prefixes(ids)[source_id]


def resolve_sample(store: LabStore, text: str) -> str:
    """``latest`` or an ``id12`` prefix -> ``source_id`` (TBX-LAB-017)."""

    ids = source_ids(store)
    if text == "latest":
        records = [(source_record(store, sid) or {}, sid) for sid in ids]
        dated = [
            (max((entry.get("used_utc", "") for entry in record.get("inputs", [])), default=""), sid)
            for record, sid in records
        ]
        if not dated:
            raise LabProblem(
                "TBX-LAB-017",
                cause="no Lab sample under this ROOT yet",
                fix="Run `traceback lab run <chunk dir>` first; pass the same --root",
                data={"candidates": []},
            )
        return max(dated)[1]
    match = resolve_prefix(text, ids)
    if match.state == "found":
        assert match.value is not None
        return match.value
    candidates = [shortest_unique_prefixes(ids)[value] for value in match.candidates] if match.candidates else []
    raise LabProblem(
        "TBX-LAB-017",
        cause=(
            f"sample prefix {text!r} is ambiguous" if match.state == "ambiguous" else f"no Lab sample matches {text!r}"
        ),
        fix="Use a longer prefix; `traceback lab runs` lists samples",
        data={"candidates": candidates},
    )


def is_sample_reference(text: str) -> bool:
    return text == "latest" or bool(re.fullmatch(r"[0-9a-f]{12,64}", text))


__all__ = [
    "AlignedInput",
    "Budget",
    "Chunk",
    "ChunkSet",
    "EXAMPLE_REFERENCE_ID",
    "Reference",
    "align",
    "aligner_argv_identity",
    "alignment_stages",
    "check_sq",
    "chunk_index",
    "collect_paths",
    "count_records",
    "disk_budget",
    "ensure_mmi",
    "estimate_align_minutes",
    "id12_for",
    "ingest_prealigned",
    "input_manifest",
    "inspect_chunks",
    "require_unchanged",
    "is_sample_reference",
    "mmi_path",
    "require_budget",
    "resolve_reference",
    "resolve_sample",
    "source_ids",
    "source_record",
    "write_source",
]
