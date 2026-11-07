"""``traceback lab inventory DIR [--recursive] [--report PATH] [--no-hash] [--json]``: is a kit run's chunk set whole?

A read-only check of a MinKNOW output directory *before* alignment (the
spec's step S0: the chunk count, read count and basecaller/protocol provenance
of the whole run, with checksums).  ``lab run`` refuses truncated and
duplicate chunks only at ingest; this command finds a partial download first.

Chunks are grouped by their immediate directory: DIR itself, or with
``--recursive`` every directory under it that holds indexed ``*.bam`` files.
Index coverage and same-index duplicates are judged per group; ``bam_fail*``
(and ``fastq_fail*``/``pod5_fail*``) directories, inside DIR or beside a
``bam_pass`` DIR, are walked the same way but only counted, never
inventoried.  Every directory visited takes a number from one counter, and a
group is labelled by its directory names only when every name is a MinKNOW
one (``bam_pass``, ``barcodeNN``, ``unclassified`` ...); any other directory
is shown as ``group N (sha256 …)``, because MinKNOW's run layout carries the
sample id in directory names.

For every indexed ``<stem>_<n>.bam`` chunk it records the size, whether the
28-byte BGZF EOF block is present and ``samtools quickcheck -u`` passes, the
index, the header's provenance (``@RG`` basecall and modified-base models,
``@PG`` program versions; the run id and sample id are reported only as
*present* plus a digest prefix, never as values), its sha256 (the Lab hash
cache is read, never written; ``--no-hash`` skips hashing and with it the
duplicate-content check) and its record count: exact from the cache or when
the whole file is read, otherwise an estimate from the first records
extrapolated by compressed size and labelled as such.  Hashing streams the
compressed bytes; an exact count would decompress every record, several
times slower, so the estimate is kept unless the cache already holds the
count.

A MinKNOW ``final_summary*.txt`` or ``sequencing_summary*.txt`` beside the
chunks or in the parent run directory (exactly one per pattern, never a
symlink), or named with ``--report PATH``, supplies the run's declared
BAM-file and read counts.  A report is compared only when its run id matches
the chunks' ``@RG`` run id; with neither side declaring one, only an explicit
``--report`` is compared.  The verdict is ``COMPLETE`` (a comparable report
agrees with the disk), ``INCOMPLETE`` (missing indices, or a report declares
more than is on disk), ``DAMAGED`` (truncated chunks, duplicates, or files a
report does not declare) or ``UNKNOWN`` (nothing comparable).

Printed names: ROOT, DIR as given, MinKNOW directory names, indexed chunk
basenames, and a report basename only when it has MinKNOW's
``final_summary_…txt`` / ``sequencing_summary_…txt`` shape; any other file
(an unindexed ``.bam``, an operator-named report) appears as a sha256 prefix.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam

from traceback_runner.cli import _local_envelope, _result, _with_human

from .ingest import (
    _order_key,
    _quickcheck,
    _rg_model,
    _sha256_file,
    chunk_index,
    count_records,
    has_bgzf_eof,
    read_header_lines,
)
from .problems import EXIT_OK, EXIT_PARTIAL, LabProblem
from .store import LabStore

# Records read before extrapolating, and the compressed bytes those records
# must span; a chunk that ends first is counted exactly.
SAMPLE_RECORDS = 2_000
SAMPLE_BYTES = 1024 * 1024
SAMPLE_RECORDS_MAX = 20_000
DIGEST_PREFIX = 12
REPORT_PATTERNS = (("final_summary", "final_summary*.txt"), ("sequencing_summary", "sequencing_summary*.txt"))
REPORT_NAME = re.compile(r"^(final|sequencing)_summary_[A-Za-z0-9_.-]+\.txt$")

KNOWN_DIR = re.compile(r"^(?:(?:bam|fastq|pod5)_(?:pass|fail)|barcode\d+|unclassified)$")
FAIL_DIR = re.compile(r"^(?:bam|fastq|pod5)_fail")
CHUNK_NAME = re.compile(r"^[A-Za-z0-9._-]+\.bam$")

VERDICT_COMPLETE = "COMPLETE"
VERDICT_INCOMPLETE = "INCOMPLETE"
VERDICT_DAMAGED = "DAMAGED"
VERDICT_UNKNOWN = "UNKNOWN"

_DS_FIELD = re.compile(r"(\w+)=([^\s]+)")


def add_inventory_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    inventory = commands.add_parser(
        "inventory", help="check a MinKNOW chunk directory for completeness before alignment (read-only)"
    )
    inventory.add_argument("directory", metavar="DIR", help="directory of MinKNOW uBAM chunks (*.bam)")
    inventory.add_argument("--recursive", action="store_true", help="also scan subdirectories (bam_pass/, barcodeNN/ ...)")
    inventory.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="a MinKNOW final_summary*.txt or sequencing_summary*.txt to compare against (auto-detected otherwise)",
    )
    inventory.add_argument(
        "--no-hash", dest="hash", action="store_false",
        help="skip sha256 hashing (and so the duplicate-content check); reads are still counted",
    )
    common(inventory)
    inventory.set_defaults(handler=inventory_command)


# -- chunks ---------------------------------------------------------------------------
@dataclass
class Entry:
    """One indexed ``*.bam`` chunk; ``name`` is its basename, ``group`` its directory group."""

    name: str
    path: Path
    group: int
    index: int
    size: int
    eof_marker: bool = False
    quickcheck: bool = False
    header_ok: bool = False
    aligned: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    sha256: str | None = None
    hash_failed: bool = False
    records: int | None = None
    records_kind: str | None = None  # "exact" | "estimate"
    sampled_records: int | None = None
    cached: bool = False

    @property
    def truncated(self) -> bool:
        return not (self.eof_marker and self.quickcheck and self.header_ok)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "index": self.index,
            "bytes": self.size,
            "eof_marker": self.eof_marker,
            "quickcheck": self.quickcheck,
            "header_ok": self.header_ok,
            "truncated": self.truncated,
            "aligned": self.aligned,
            "sha256": self.sha256,
            "hash_failed": self.hash_failed,
            "records": self.records,
            "records_kind": self.records_kind,
            "sampled_records": self.sampled_records,
            "hash_cached": self.cached,
            "provenance": dict(self.provenance),
        }


def _digest_prefix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_PREFIX]


def group_label(number: int, parts: Sequence[str]) -> str:
    """``DIR`` for the top level, the MinKNOW directory names when every name is one, else a digest."""

    if not parts:
        return "DIR"
    if all(KNOWN_DIR.match(part) for part in parts):
        return "/".join(parts)
    return f"group {number} (sha256 {_digest_prefix('/'.join(parts))})"


@dataclass
class Group:
    """One directory holding chunks; index coverage and same-index duplicates are judged here."""

    id: int
    parts: tuple[str, ...]
    entries: list[Entry] = field(default_factory=list)
    unindexed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return all(KNOWN_DIR.match(part) for part in self.parts)

    @property
    def label(self) -> str:
        return group_label(self.id, self.parts)

    @property
    def indexes(self) -> list[int]:
        return sorted(entry.index for entry in self.entries)

    @property
    def gaps(self) -> list[tuple[int, int]]:
        return missing_ranges(self.indexes)

    def public(self) -> dict[str, Any]:
        indexes = self.indexes
        gaps = self.gaps
        return {
            "id": self.id,
            "label": self.label,
            "known": self.known,
            "depth": len(self.parts),
            "path_sha256_12": _digest_prefix("/".join(self.parts)) if self.parts else None,
            "chunks": len(self.entries),
            "bytes": sum(entry.size for entry in self.entries),
            "index_min": indexes[0] if indexes else None,
            "index_max": indexes[-1] if indexes else None,
            "missing_indexes": [list(gap) for gap in gaps],
            "missing_index_count": sum(b - a + 1 for a, b in gaps),
            "unindexed": list(self.unindexed),
        }


@dataclass(frozen=True)
class FailGroup:
    """A fail-output directory: indexed regular ``.bam`` files counted, never inventoried."""

    id: int
    label: str
    bam_files: int
    unindexed: int
    where: str  # "DIR" | "sibling"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "bam_files": self.bam_files, "unindexed": self.unindexed, "where": self.where}


@dataclass(frozen=True)
class Skipped:
    label: str
    name: str | None
    kind: str

    def public(self) -> dict[str, Any]:
        return {"group": self.label, "name": self.name, "kind": self.kind}

    def text(self) -> str:
        return f"{self.label}: {self.name} ({self.kind})" if self.name else f"{self.label} ({self.kind})"


@dataclass
class Scan:
    groups: list[Group]
    fail_groups: list[FailGroup]
    skipped: list[Skipped]

    @property
    def entries(self) -> list[Entry]:
        return [entry for group in self.groups for entry in group.entries]

    @property
    def on_disk_files(self) -> int:
        """Indexed regular ``.bam`` files, pass and fail alike (unindexed and skipped excluded on both sides)."""

        return len(self.entries) + sum(group.bam_files for group in self.fail_groups)


def _refuse_path(argument: str, kind: str, shown: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-005",
        cause=f"{argument} ({shown}) is {kind}",
        fix="Pass the real file or directory (realpath), or fix its permissions",
        data={"argument": argument, "kind": kind},
    )


class _Walker:
    """One directory walk; every directory visited takes a number from one counter."""

    def __init__(self, *, recursive: bool) -> None:
        self.recursive = recursive
        self.counter = 0
        self.groups: list[Group] = []
        self.fails: list[FailGroup] = []
        self.skipped: list[Skipped] = []

    def visit(self, folder: Path, parts: tuple[str, ...], *, fail: bool, where: str, top: Path | None = None) -> None:
        self.counter += 1
        number = self.counter
        label = group_label(number, parts)
        try:
            names = sorted(os.listdir(folder))
        except OSError as exc:
            if top is not None:
                raise _refuse_path("DIR", "unreadable", top.name or str(top)) from exc
            self.skipped.append(Skipped(label, None, "unreadable directory"))
            return
        fail = fail or (bool(parts) and any(FAIL_DIR.match(part) for part in parts))
        entries: list[Entry] = []
        unindexed: list[dict[str, Any]] = []
        subdirs: list[str] = []
        for name in names:
            if name.startswith("."):
                continue
            path = folder / name
            try:
                info = os.lstat(path)
            except OSError:
                self.skipped.append(Skipped(label, name if name.endswith(".bam") else None, "unreadable"))
                continue
            if stat.S_ISLNK(info.st_mode):
                if name.endswith(".bam"):
                    self.skipped.append(Skipped(label, name, "a symlink"))
                elif self.recursive and path.is_dir():
                    self.counter += 1
                    self.skipped.append(Skipped(group_label(self.counter, (*parts, name)), None, "a symlink, not followed"))
                continue
            if stat.S_ISDIR(info.st_mode):
                if self.recursive:
                    subdirs.append(name)
                continue
            if not name.endswith(".bam"):
                continue
            if not stat.S_ISREG(info.st_mode):
                self.skipped.append(Skipped(label, name, "not a regular file"))
            elif not os.access(path, os.R_OK):
                self.skipped.append(Skipped(label, name, "unreadable"))
            elif (index := chunk_index(name)) is None:
                unindexed.append({"name_sha256_12": _digest_prefix(name), "bytes": info.st_size})
            else:
                entries.append(Entry(name=name, path=path, group=number, index=index, size=info.st_size))
        if entries or unindexed:
            if fail:
                self.fails.append(FailGroup(number, label, len(entries), len(unindexed), where))
            else:
                entries.sort(key=lambda entry: _order_key(entry.path))
                self.groups.append(Group(id=number, parts=parts, entries=entries, unindexed=unindexed))
        for sub in subdirs:
            self.visit(folder / sub, (*parts, sub), fail=fail, where=where)


def scan_directory(directory: Path, *, recursive: bool) -> Scan:
    """Group regular readable indexed ``*.bam`` files by directory; everything else is listed, not counted.

    A ``bam_fail`` beside a ``bam_pass`` DIR is walked recursively (barcoded
    runs keep fail chunks in ``bam_fail/barcodeNN/``) and counted the same way
    as the pass chunks: indexed regular files only.
    """

    walker = _Walker(recursive=recursive)
    walker.visit(directory, (), fail=False, where="DIR", top=directory)
    if directory.name == "bam_pass":
        sibling = directory.parent / "bam_fail"
        try:
            info = os.lstat(sibling)
        except OSError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            sibling_walker = _Walker(recursive=True)
            sibling_walker.counter = walker.counter
            sibling_walker.visit(sibling, ("bam_fail",), fail=True, where="sibling")
            walker.fails.extend(sibling_walker.fails)
            walker.skipped.extend(sibling_walker.skipped)
        elif info is not None and stat.S_ISLNK(info.st_mode):
            walker.counter += 1
            walker.skipped.append(Skipped("bam_fail", None, "a symlink, not followed"))
    return Scan(groups=walker.groups, fail_groups=walker.fails, skipped=walker.skipped)


def _tags(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in line.split("\t")[1:]:
        tag, sep, value = item.partition(":")
        if sep:
            fields[tag] = value
    return fields


def header_provenance(rg_lines: Sequence[str], pg_lines: Sequence[str]) -> dict[str, Any]:
    """Models and program versions from the header; identifiers only as presence + digest prefix."""

    models: set[str] = set()
    modbase: set[str] = set()
    run_ids: set[str] = set()
    sample_ids: set[str] = set()
    for line in rg_lines:
        tags = _tags(line)
        if model := _rg_model(line):
            models.add(model)
        for key, value in _DS_FIELD.findall(tags.get("DS", "")):
            if key == "modbase_models":
                modbase.update(part for part in value.split(",") if part)
            elif key == "runid":
                run_ids.add(value)
        for key in ("SM", "LB"):
            if tags.get(key):
                sample_ids.add(tags[key])
    programs: list[dict[str, str | None]] = []
    for line in pg_lines:
        tags = _tags(line)
        name = tags.get("PN") or tags.get("ID")
        if name and not any(p["name"] == name and p["version"] == tags.get("VN") for p in programs):
            programs.append({"name": name, "version": tags.get("VN")})
    return {
        "basecall_models": sorted(models),
        "modbase_models": sorted(modbase),
        "programs": programs,
        "read_groups": len(rg_lines),
        "run_id": {"present": bool(run_ids), "sha256_12": sorted(_digest_prefix(v) for v in run_ids)},
        "sample_id": {"present": bool(sample_ids), "sha256_12": sorted(_digest_prefix(v) for v in sample_ids)},
    }


def estimate_records(path: Path, size: int) -> tuple[int, str, int]:
    """(records, kind, records read) for one BAM: exact when the whole file is read, else extrapolated.

    The rate is records wholly before a BGZF block boundary over that boundary's
    compressed offset, so a record straddling blocks never skews it.  Hashing
    streams compressed bytes and cannot count records; an exact count would
    decompress the whole file, several times slower, so it is not attempted.
    """

    with pysam.AlignmentFile(str(path), "rb", check_sq=False) as bam:
        seen = 0
        before_block = 0
        block_start = 0
        for _ in bam:
            seen += 1
            block = bam.tell() >> 16
            if block != block_start:
                before_block, block_start = seen, block
                if seen >= SAMPLE_RECORDS and block_start >= SAMPLE_BYTES:
                    break
            if seen >= SAMPLE_RECORDS_MAX and block_start > 0:
                break
        else:
            return seen, "exact", seen
    if block_start <= 0:
        exact = count_records(path)
        return exact, "exact", exact
    return max(seen, round(before_block * size / block_start)), "estimate", seen


def inspect_entries(
    entries: Sequence[Entry], store: LabStore, *, hashing: bool = True,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Fill every entry in place: integrity, header provenance, sha256 (cache read-only) and records."""

    cache = store.load_hash_cache()
    for number, entry in enumerate(entries, start=1):
        entry.eof_marker = has_bgzf_eof(entry.path)
        entry.quickcheck = _quickcheck(entry.path)
        try:
            rg, pg, sq = read_header_lines(entry.path)
        except (OSError, ValueError):
            rg, pg, sq = (), (), ()
        else:
            entry.header_ok = True
            entry.aligned = bool(sq)
            entry.provenance = header_provenance(rg, pg)
        try:
            cached = cache.get(store.hash_cache_key(entry.path))
        except OSError:
            cached = None
        if cached and isinstance(cached.get("sha256"), str) and isinstance(cached.get("records"), int):
            entry.sha256 = cached["sha256"]
            entry.records = cached["records"]
            entry.records_kind = "exact"
            entry.sampled_records = cached["records"]
            entry.cached = True
            continue
        if hashing:
            if progress is not None:
                progress(f"inventory  hashing chunk {number}/{len(entries)}")
            try:
                entry.sha256 = _sha256_file(entry.path)
            except OSError:
                entry.sha256 = None
                entry.hash_failed = True
        if entry.truncated:
            continue
        try:
            entry.records, entry.records_kind, entry.sampled_records = estimate_records(entry.path, entry.size)
        except Exception:  # noqa: BLE001 - an unreadable body is reported, not raised
            entry.records, entry.records_kind, entry.sampled_records = None, None, None


# -- coverage and duplicates ------------------------------------------------------------
def missing_ranges(indexes: Iterable[int]) -> list[tuple[int, int]]:
    """Gaps in ``0..max`` as inclusive ranges."""

    present = set(indexes)
    if not present:
        return []
    gaps: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(max(present) + 1):
        if index in present:
            if start is not None:
                gaps.append((start, index - 1))
                start = None
        elif start is None:
            start = index
    if start is not None:
        gaps.append((start, max(present)))
    return gaps


def format_ranges(ranges: Sequence[tuple[int, int]]) -> str:
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def _ref(entry: Entry) -> dict[str, Any]:
    return {"group": entry.group, "name": entry.name}


def duplicates(groups: Sequence[Group], *, hashing: bool = True) -> list[dict[str, Any]]:
    """Same content anywhere (when hashed); the same index twice within one group."""

    found: list[dict[str, Any]] = []
    by_hash: dict[str, Entry] = {}
    for group in groups:
        by_index: dict[int, Entry] = {}
        for entry in group.entries:
            if hashing and entry.sha256:
                if entry.sha256 in by_hash:
                    found.append({
                        "kind": "same_content",
                        "chunks": [_ref(by_hash[entry.sha256]), _ref(entry)],
                        "sha256_12": entry.sha256[:DIGEST_PREFIX],
                    })
                else:
                    by_hash[entry.sha256] = entry
            other = by_index.setdefault(entry.index, entry)
            if other is not entry and not (hashing and entry.sha256 and other.sha256 == entry.sha256):
                found.append({"kind": "same_index", "chunks": [_ref(other), _ref(entry)], "index": entry.index})
    return found


# -- MinKNOW reports ------------------------------------------------------------------------
def _is_key_value(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            first = stream.readline()
    except OSError:
        return False
    return "=" in first and "\t" not in first


def sanitize_chunk_name(cell: str) -> str | None:
    """A report cell as a chunk basename, or None when it is not shaped like one."""

    name = cell.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return name if CHUNK_NAME.match(name) else None


def report_label(name: str, kind: str) -> str:
    """A MinKNOW-shaped report basename as is; any other name as its kind plus a digest."""

    if REPORT_NAME.match(name):
        return name
    return f"{kind} (sha256 {_digest_prefix(name)})"


def parse_final_summary(path: Path) -> dict[str, Any]:
    """``key=value`` lines: BAM-file counts and run-id digests only; identifier values are never kept."""

    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            key, sep, value = line.rstrip("\n").partition("=")
            if sep:
                values[key.strip()] = value.strip()

    def count(key: str) -> int | None:
        try:
            return int(values[key])
        except (KeyError, ValueError):
            return None

    final = count("bam_files_in_final_dest")
    fallback = count("bam_files_in_fallback")
    run_ids = {values[key] for key in ("acquisition_run_id", "protocol_run_id") if values.get(key)}
    return {
        "kind": "final_summary",
        "declared_bam_files": None if final is None else final + (fallback or 0),
        "declared_bam_files_in_fallback": fallback,
        "basecalling_enabled": values.get("basecalling_enabled"),
        "sample_id_present": bool(values.get("sample_id")),
        "run_id": {"present": bool(run_ids), "sha256_12": sorted(_digest_prefix(v) for v in run_ids)},
        "keys": len(values),
    }


def parse_sequencing_summary(path: Path) -> dict[str, Any]:
    """One row per read: read totals, pass totals and pass chunk basenames when the columns exist."""

    total = passed = 0
    pass_files: set[str] = set()
    all_files: set[str] = set()
    run_ids: set[str] = set()
    unparseable = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        header = stream.readline().rstrip("\n").split("\t")
        columns = {name: position for position, name in enumerate(header)}
        file_column = columns.get("filename_bam")
        pass_column = columns.get("passes_filtering")
        run_column = columns.get("run_id")
        for line in stream:
            if not line.strip():
                continue
            cells = line.rstrip("\n").split("\t")
            total += 1
            if run_column is not None and len(cells) > run_column and cells[run_column]:
                run_ids.add(cells[run_column])
            name = None
            if file_column is not None and len(cells) > file_column and cells[file_column]:
                name = sanitize_chunk_name(cells[file_column])
                if name is None:
                    unparseable += 1
                else:
                    all_files.add(name)
            if pass_column is not None and len(cells) > pass_column and cells[pass_column].upper() == "TRUE":
                passed += 1
                if name:
                    pass_files.add(name)
    has_pass = pass_column is not None
    return {
        "kind": "sequencing_summary",
        "declared_reads": total,
        "declared_pass_reads": passed if has_pass else None,
        "declared_bam_files": len(all_files) if file_column is not None else None,
        "declared_pass_bam_files": sorted(pass_files) if (has_pass and file_column is not None) else None,
        "unparseable_names": unparseable,
        "passes_filtering_column": has_pass,
        "run_id": {"present": bool(run_ids), "sha256_12": sorted(_digest_prefix(v) for v in run_ids)},
    }


def _regular_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def find_reports(directory: Path, explicit: Path | None) -> list[dict[str, Any]]:
    """``--report`` as given (TBX-LAB-005 when it is not a regular readable file), else auto-detection.

    Auto-detection looks in DIR, then its parent, for ``final_summary*.txt``
    and ``sequencing_summary*.txt``: symlinks are ignored and a pattern with
    several matches is reported as ambiguous, never picked from.
    """

    if explicit is not None:
        path = Path(os.path.abspath(explicit.expanduser()))
        shown = explicit.name or str(explicit)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise _refuse_path("--report", "missing", shown) from None
        except OSError:
            raise _refuse_path("--report", "unreadable", shown) from None
        if stat.S_ISLNK(info.st_mode):
            raise _refuse_path("--report", "a symlink", shown)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse_path("--report", "not a regular file", shown)
        if not os.access(path, os.R_OK):
            raise _refuse_path("--report", "unreadable", shown)
        return [{"path": path, "where": "--report", "explicit": True}]
    found: list[dict[str, Any]] = []
    for folder, where in ((directory, "DIR"), (directory.parent, "parent directory")):
        for kind, pattern in REPORT_PATTERNS:
            matches = sorted(path for path in folder.glob(pattern) if _regular_file(path))
            if len(matches) == 1:
                found.append({"path": matches[0], "where": where, "explicit": False})
            elif matches:
                found.append({"ambiguous": kind, "candidates": len(matches), "where": where})
        if found:
            break
    return found


def read_report(spec: dict[str, Any]) -> dict[str, Any]:
    """Parse one report by name, else by shape; an unreadable report is reported, not raised."""

    if "ambiguous" in spec:
        kind = spec["ambiguous"]
        return {
            "name": f"{kind} (ambiguous)", "name_sha256_12": None, "kind": "ambiguous", "pattern": kind,
            "candidates": spec["candidates"], "where": spec["where"], "explicit": False,
        }
    path: Path = spec["path"]
    name = path.name
    try:
        if name.startswith("final_summary") or (not name.startswith("sequencing_summary") and _is_key_value(path)):
            parsed = parse_final_summary(path)
        else:
            parsed = parse_sequencing_summary(path)
    except OSError as exc:
        parsed = {"kind": "unreadable", "error": type(exc).__name__}
    return {
        "name": report_label(name, parsed["kind"]),
        "name_sha256_12": _digest_prefix(name),
        "where": spec["where"],
        "explicit": bool(spec.get("explicit")),
        **parsed,
    }


# -- comparison and verdict -------------------------------------------------------------------
def compare_reports(
    reports: Sequence[dict[str, Any]], scan: Scan, chunk_run_ids: set[str],
) -> list[dict[str, Any]]:
    """Each report against the disk.

    ``comparable`` is False when the report's run id does not match the chunks'
    ``@RG`` run id, when neither side declares one (unless the report was named
    with ``--report``), or when the report lacks what a comparison needs;
    ``agrees`` is None then.  ``incomplete``/``damaged`` are verdict reasons,
    ``notes`` are not.
    """

    entries = scan.entries
    on_disk_names = {entry.name for entry in entries}
    on_disk_files = scan.on_disk_files
    results: list[dict[str, Any]] = []
    for report in reports:
        label = report["name"]
        comparison: dict[str, Any] = {
            "name": label, "kind": report["kind"], "run_id_match": None,
            "comparable": False, "agrees": None, "incomplete": [], "damaged": [], "notes": [],
        }
        if report["kind"] == "ambiguous":
            comparison["notes"].append(
                f"ambiguous reports: {report['candidates']} {report['pattern']} files in {report['where']}; not compared"
            )
            results.append(comparison)
            continue
        if report["kind"] == "unreadable":
            comparison["notes"].append(f"{label}: unreadable")
            results.append(comparison)
            continue
        report_ids = set(report.get("run_id", {}).get("sha256_12", ()))
        if report_ids and chunk_run_ids:
            comparison["run_id_match"] = bool(report_ids & chunk_run_ids)
        if comparison["run_id_match"] is False:
            comparison["notes"].append(f"{label}: report run id does not match chunks; not compared")
            results.append(comparison)
            continue
        if comparison["run_id_match"] is None:
            side = "report" if not report_ids else "chunks"
            if not report_ids and not chunk_run_ids:
                side = "report and chunks"
            if report.get("explicit"):
                comparison["notes"].append(f"{label}: run id not verified (explicit --report)")
            else:
                comparison["notes"].append(f"{label}: run id not declared on {side}; not compared")
                results.append(comparison)
                continue
        if report["kind"] == "final_summary":
            declared = report.get("declared_bam_files")
            if declared is None:
                comparison["notes"].append(f"{label}: no bam_files_in_final_dest count; not comparable")
            else:
                comparison["comparable"] = True
                comparison["declared_bam_files"] = declared
                comparison["on_disk_bam_files"] = on_disk_files
                comparison["agrees"] = declared == on_disk_files
                where = " (indexed chunks plus fail output)" if scan.fail_groups else ""
                if declared > on_disk_files:
                    comparison["incomplete"].append(
                        f"{label} declares {declared} BAM file(s); {on_disk_files} on disk{where}"
                    )
                elif declared < on_disk_files:
                    comparison["damaged"].append(
                        f"{on_disk_files} BAM file(s) on disk{where}; {label} declares {declared}"
                    )
        elif report["kind"] == "sequencing_summary":
            comparison["declared_reads"] = report.get("declared_reads")
            comparison["declared_pass_reads"] = report.get("declared_pass_reads")
            if not report.get("passes_filtering_column"):
                comparison["notes"].append(f"{label}: no passes_filtering column; not comparable")
            elif report.get("declared_pass_bam_files") is None:
                comparison["notes"].append(f"{label}: no filename_bam column; not comparable")
            else:
                declared_names = set(report["declared_pass_bam_files"])
                missing = sorted(declared_names - on_disk_names)
                extra = sorted(on_disk_names - declared_names)
                comparison["comparable"] = True
                comparison["declared_pass_bam_files"] = len(declared_names)
                comparison["missing_on_disk"] = missing
                comparison["not_declared"] = extra
                if missing:
                    comparison["incomplete"].append(f"{label} lists {len(missing)} pass chunk(s) not on disk")
                if extra:
                    comparison["damaged"].append(f"{len(extra)} chunk(s) on disk that {label} does not list")
                if report.get("unparseable_names"):
                    comparison["notes"].append(f"{label}: {report['unparseable_names']} filename_bam cell(s) not chunk-shaped, ignored")
                unreadable = sum(entry.records is None for entry in entries)
                estimated = sum(entry.records_kind == "estimate" for entry in entries)
                if unreadable:
                    comparison["notes"].append(f"read totals not compared ({unreadable} chunk(s) unreadable)")
                elif estimated:
                    comparison["notes"].append(f"read totals not compared ({estimated} chunk count(s) are estimates)")
                elif entries and not missing and not extra:
                    on_disk_reads = sum(entry.records or 0 for entry in entries)
                    comparison["on_disk_reads"] = on_disk_reads
                    if on_disk_reads != report["declared_pass_reads"]:
                        comparison["incomplete"].append(
                            f"{label} declares {report['declared_pass_reads']:,} pass read(s); {on_disk_reads:,} in the chunks on disk"
                        )
                comparison["agrees"] = not comparison["incomplete"] and not comparison["damaged"]
        results.append(comparison)
    return results


def verdict_for(scan: Scan, found_duplicates: Sequence[dict[str, Any]] | None,
                comparisons: Sequence[dict[str, Any]]) -> tuple[str, list[str]]:
    damaged: list[str] = []
    incomplete: list[str] = []
    truncated = [entry for entry in scan.entries if entry.truncated]
    if truncated:
        damaged.append(f"{len(truncated)} truncated chunk(s)")
    if found_duplicates:
        damaged.append(f"{len(found_duplicates)} duplicate(s)")
    for comparison in comparisons:
        damaged.extend(comparison["damaged"])
    if damaged:
        return VERDICT_DAMAGED, damaged
    for group in scan.groups:
        gaps = group.gaps
        if gaps:
            missing = sum(b - a + 1 for a, b in gaps)
            prefix = f"{group.label}: " if len(scan.groups) > 1 or group.parts else ""
            incomplete.append(f"{prefix}missing {missing} index(es): {format_ranges(gaps)}")
    for comparison in comparisons:
        incomplete.extend(comparison["incomplete"])
    if incomplete:
        return VERDICT_INCOMPLETE, incomplete
    if any(comparison["agrees"] for comparison in comparisons):
        return VERDICT_COMPLETE, ["a MinKNOW report agrees with the chunks on disk"]
    notes = [note for comparison in comparisons for note in comparison["notes"]]
    if notes:
        return VERDICT_UNKNOWN, notes
    return VERDICT_UNKNOWN, ["no MinKNOW report to compare against (contiguous indices alone do not prove the run is whole)"]


# -- command ------------------------------------------------------------------------------
def _merge_provenance(entries: Sequence[Entry]) -> dict[str, Any]:
    models: set[str] = set()
    modbase: set[str] = set()
    programs: list[dict[str, str | None]] = []
    run_ids: set[str] = set()
    sample_ids: set[str] = set()
    for entry in entries:
        prov = entry.provenance
        if not prov:
            continue
        models.update(prov["basecall_models"])
        modbase.update(prov["modbase_models"])
        for program in prov["programs"]:
            if program not in programs:
                programs.append(program)
        run_ids.update(prov["run_id"]["sha256_12"])
        sample_ids.update(prov["sample_id"]["sha256_12"])
    return {
        "basecall_models": sorted(models),
        "modbase_models": sorted(modbase),
        "programs": programs,
        "run_id": {"present": bool(run_ids), "sha256_12": sorted(run_ids)},
        "sample_id": {"present": bool(sample_ids), "sha256_12": sorted(sample_ids)},
    }


def _fmt_ids(block: dict[str, Any]) -> str:
    if not block["present"]:
        return "absent"
    return "present (sha256 " + ", ".join(block["sha256_12"]) + ")"


def _chunk_text(ref: dict[str, Any], labels: dict[int, str]) -> str:
    label = labels.get(ref["group"], "?")
    return ref["name"] if label == "DIR" else f"{label}/{ref['name']}"


def inventory_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    given = str(args.directory)
    directory = Path(os.path.abspath(Path(given).expanduser()))
    shown = Path(given).name or given
    hashing = bool(getattr(args, "hash", True))
    recursive = bool(args.recursive)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise _refuse_path("DIR", "missing", shown) from None
    except OSError:
        raise _refuse_path("DIR", "unreadable", shown) from None
    if stat.S_ISLNK(info.st_mode):
        raise _refuse_path("DIR", "a symlink", shown)
    if not stat.S_ISDIR(info.st_mode):
        raise _refuse_path("DIR", "not a directory", shown)
    report_specs = find_reports(directory, args.report)

    scan = scan_directory(directory, recursive=recursive)
    entries = scan.entries
    if not entries:
        raise LabProblem(
            "TBX-LAB-031",
            cause=f"DIR ({shown}) holds no readable indexed .bam chunk" + (" (searched recursively)" if recursive else ""),
            fix="Point at the MinKNOW bam_pass directory, or add --recursive for a run folder",
            data={
                "dir": given,
                "skipped": [s.public() for s in scan.skipped],
                "fail_groups": [g.public() for g in scan.fail_groups],
                "unindexed": [{"group": g.id, **u} for g in scan.groups for u in g.unindexed],
            },
        )

    def progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    inspect_entries(entries, store, hashing=hashing, progress=progress)
    found_duplicates = duplicates(scan.groups, hashing=hashing)
    hash_failures = sum(entry.hash_failed for entry in entries)
    content_checked = hashing and not hash_failures
    provenance = _merge_provenance(entries)
    reports = [read_report(spec) for spec in report_specs]
    comparisons = compare_reports(reports, scan, set(provenance["run_id"]["sha256_12"]))
    verdict, reasons = verdict_for(scan, found_duplicates, comparisons)

    total_bytes = sum(entry.size for entry in entries)
    counted = [entry for entry in entries if entry.records is not None]
    estimated = any(entry.records_kind == "estimate" for entry in counted)
    total_records = sum(entry.records for entry in counted) if counted else None
    labels = {group.id: group.label for group in scan.groups}
    unindexed = [{"group": group.id, **item} for group in scan.groups for item in group.unindexed]
    missing_total = sum(group.public()["missing_index_count"] for group in scan.groups)

    data: dict[str, Any] = {
        "root": str(store.root),
        "dir": given,
        "recursive": recursive,
        "hashing": hashing,
        "verdict": verdict,
        "reasons": reasons,
        "chunks": len(entries),
        "total_bytes": total_bytes,
        "groups": [group.public() for group in scan.groups],
        "missing_index_count": missing_total,
        "unindexed": unindexed,
        "truncated": [_ref(entry) for entry in entries if entry.truncated],
        "duplicates": found_duplicates,
        "duplicate_content_checked": content_checked,
        "hash_failures": hash_failures,
        "records": {
            "total": total_records,
            "kind": None if total_records is None else ("estimate" if estimated else "exact"),
            "counted_chunks": len(counted),
            "unreadable_chunks": len(entries) - len(counted),
            "sample_records": SAMPLE_RECORDS,
            "sample_bytes": SAMPLE_BYTES,
        },
        "provenance": provenance,
        "hash_cached_chunks": sum(entry.cached for entry in entries),
        "fail_groups": [group.public() for group in scan.fail_groups],
        "on_disk_bam_files": scan.on_disk_files,
        "reports": reports,
        "comparisons": comparisons,
        "skipped": [s.public() for s in scan.skipped],
        "entries": [entry.public() for entry in entries],
    }

    lines = [f"ROOT  {store.root}", f"DIR   {given}"]
    first, last = entries[0], entries[-1]
    lines.append(
        f"CHUNKS      {len(entries)}  ({_chunk_text(_ref(first), labels)}"
        + (f" … {_chunk_text(_ref(last), labels)}" if len(entries) > 1 else "")
        + f")  {total_bytes:,} bytes"
        + (f" in {len(scan.groups)} group(s)" if len(scan.groups) > 1 else "")
    )
    for group in scan.groups:
        public = group.public()
        indexes = group.indexes
        prefix = f"{group.label}: " if len(scan.groups) > 1 or group.parts else ""
        if indexes:
            text = f"{indexes[0]}-{indexes[-1]} " + (
                "contiguous" if not group.gaps
                else f"missing {format_ranges(group.gaps)} ({public['missing_index_count']} index(es))"
            )
        else:
            text = "no indexed chunk"
        lines.append(f"INDICES     {prefix}{text}")
    if unindexed:
        lines.append(
            f"UNINDEXED   {len(unindexed)} .bam file(s) without a _<n>.bam index, not counted (sha256 "
            + ", ".join(item["name_sha256_12"] for item in unindexed) + ")"
        )
    truncated = data["truncated"]
    lines.append("TRUNCATED   " + (", ".join(_chunk_text(ref, labels) for ref in truncated) if truncated else "none"))
    duplicate_text = "; ".join(
        f"{' = '.join(_chunk_text(ref, labels) for ref in d['chunks'])} ("
        + (f"same content, sha256 {d['sha256_12']}" if d["kind"] == "same_content"
           else f"same index {d['index']}, different content")
        + ")"
        for d in found_duplicates
    )
    if not hashing:
        lines.append("DUPLICATES  content not checked (--no-hash); the verdict cannot be DAMAGED by duplicate content"
                     + (f"; {duplicate_text}" if duplicate_text else ""))
    elif hash_failures:
        lines.append(f"DUPLICATES  {hash_failures} chunk(s) could not be hashed; duplicate content not fully checked"
                     + (f"; {duplicate_text}" if duplicate_text else ""))
    else:
        lines.append("DUPLICATES  " + (duplicate_text or "none"))
    if total_records is None:
        lines.append("READS       unavailable")
    else:
        if estimated:
            sampled = min(entry.sampled_records or 0 for entry in counted if entry.records_kind == "estimate")
            label = (f"~{total_records:,} (estimate: each estimated chunk's first records, at least "
                     f"{sampled:,}, extrapolated by compressed size")
        else:
            label = f"{total_records:,} (exact"
        exact = sum(entry.records_kind == "exact" for entry in counted)
        if estimated and exact:
            label += f"; {exact} chunk(s) exact"
        if len(counted) < len(entries):
            label += f"; {len(entries) - len(counted)} chunk(s) unreadable"
        lines.append(f"READS       {label})")
    lines.append(
        "PROVENANCE  basecall " + (", ".join(provenance["basecall_models"]) or "undeclared")
        + "; modbase " + (", ".join(provenance["modbase_models"]) or "undeclared")
        + "; programs " + (", ".join(f"{p['name']} {p['version'] or '?'}" for p in provenance["programs"]) or "none")
    )
    lines.append(f"            run id {_fmt_ids(provenance['run_id'])}; sample id {_fmt_ids(provenance['sample_id'])}")
    if data["hash_cached_chunks"]:
        lines.append(f"HASHES      {data['hash_cached_chunks']} of {len(entries)} chunk(s) from the Lab hash cache (read-only)")
    for group in scan.fail_groups:
        lines.append(f"FAIL        {group.label}{' (sibling)' if group.where == 'sibling' else ''}: "
                     f"{group.bam_files} indexed .bam file(s), fail output, not inventoried"
                     + (f"; {group.unindexed} unindexed not counted" if group.unindexed else ""))
    if scan.skipped:
        lines.append("SKIPPED     " + "; ".join(s.text() for s in scan.skipped) + "; lab run refuses these")
    if not reports:
        lines.append("REPORT      none found (final_summary*.txt or sequencing_summary*.txt in DIR or its parent; or --report PATH)")
    for report, comparison in zip(reports, comparisons):
        lines.append(f"REPORT      {report['name']} ({report['where']}): {_report_detail(report, comparison)}")
    lines.append(f"VERDICT     {verdict}: " + "; ".join(reasons))
    lines.append("")
    lines.append(_entries_table(entries, labels))

    summary = f"inventory {verdict}: {len(entries)} chunk(s), " + "; ".join(reasons)
    if verdict in {VERDICT_COMPLETE, VERDICT_UNKNOWN}:
        payload = _local_envelope(_result("lab inventory", "ok", summary, data=data))
        return EXIT_OK, _with_human(payload, lines, replace_data=True)
    payload = _local_envelope(_result("lab inventory", "partial_failure", summary, data=data))
    return EXIT_PARTIAL, _with_human(payload, lines, replace_data=True)


def _report_detail(report: dict[str, Any], comparison: dict[str, Any]) -> str:
    if report["kind"] == "ambiguous":
        return f"{report['candidates']} {report['pattern']} files; ambiguous, not compared"
    if report["kind"] == "unreadable":
        return "unreadable"
    if comparison["run_id_match"] is False:
        return "run id does not match the chunks; not compared"
    if comparison["run_id_match"] is None and not report.get("explicit"):
        return comparison["notes"][0].split(": ", 1)[1]
    suffix = "; run id not verified (explicit --report)" if comparison["run_id_match"] is None else ""
    if report["kind"] == "final_summary":
        if report.get("declared_bam_files") is None:
            return "no bam_files_in_final_dest count; not comparable" + suffix
        return f"declares {report['declared_bam_files']} BAM file(s); {comparison.get('on_disk_bam_files')} on disk" + suffix
    detail = f"declares {report['declared_reads']:,} read(s)"
    if not report.get("passes_filtering_column"):
        return detail + "; no passes_filtering column; not comparable" + suffix
    detail += f", {report['declared_pass_reads']:,} pass"
    if report.get("declared_pass_bam_files") is None:
        return detail + "; no filename_bam column; not comparable" + suffix
    detail += f", {len(report['declared_pass_bam_files'])} pass chunk(s)"
    if comparison.get("missing_on_disk"):
        detail += f"; {len(comparison['missing_on_disk'])} not on disk"
    elif comparison.get("not_declared"):
        detail += f"; {len(comparison['not_declared'])} on disk not listed"
    else:
        detail += "; names match"
    if "on_disk_reads" in comparison:
        detail += f"; {comparison['on_disk_reads']:,} read(s) on disk"
    return detail + suffix


def _entries_table(entries: Sequence[Entry], labels: dict[int, str]) -> str:
    header = ("GROUP", "INDEX", "NAME", "BYTES", "EOF", "QUICKCHECK", "SHA256", "RECORDS")
    rows = [
        (
            labels.get(entry.group, "?"),
            str(entry.index),
            entry.name,
            f"{entry.size:,}",
            "yes" if entry.eof_marker else "NO",
            "ok" if entry.quickcheck else "FAIL",
            (entry.sha256 or "")[:DIGEST_PREFIX] or ("FAILED" if entry.hash_failed else "-"),
            "-" if entry.records is None else (f"~{entry.records:,}" if entry.records_kind == "estimate" else f"{entry.records:,}"),
        )
        for entry in entries
    ]
    widths = [len(cell) for cell in header]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    render = lambda cells: "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([render(header), *(render(row) for row in rows)])


__all__ = [
    "Entry",
    "FailGroup",
    "Group",
    "REPORT_NAME",
    "SAMPLE_BYTES",
    "SAMPLE_RECORDS",
    "Scan",
    "Skipped",
    "VERDICT_COMPLETE",
    "VERDICT_DAMAGED",
    "VERDICT_INCOMPLETE",
    "VERDICT_UNKNOWN",
    "add_inventory_parser",
    "compare_reports",
    "duplicates",
    "estimate_records",
    "find_reports",
    "group_label",
    "header_provenance",
    "inspect_entries",
    "inventory_command",
    "missing_ranges",
    "parse_final_summary",
    "parse_sequencing_summary",
    "report_label",
    "sanitize_chunk_name",
    "scan_directory",
    "verdict_for",
]
