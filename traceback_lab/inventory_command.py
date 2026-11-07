"""``traceback lab inventory DIR [--recursive] [--report PATH] [--json]``: is a kit run's chunk set whole?

A read-only check of a MinKNOW output directory *before* alignment (the
spec's step S0: the chunk count, read count and basecaller/protocol provenance
of the whole run, with checksums).  ``lab run`` refuses truncated and
duplicate chunks only at ingest; this command finds a partial download first.

For every ``*.bam`` chunk it records the size, whether the 28-byte BGZF EOF
block is present and ``samtools quickcheck -u`` passes, the ``_<n>.bam``
index, the header's provenance (``@RG`` basecall and modified-base models,
``@PG`` program versions; the run id and sample id are reported only as
*present* plus a digest prefix, never as values), its sha256 (the Lab hash
cache is read, never written) and its record count (exact from the cache or
a whole read, otherwise an estimate from the first records extrapolated by
compressed size and labelled as such).

A MinKNOW ``final_summary*.txt`` or ``sequencing_summary*.txt`` beside the
chunks, in the parent run directory, or named with ``--report PATH`` supplies
the run's declared BAM-file and read counts for comparison.  The verdict is
``COMPLETE`` (a report agrees with the disk), ``INCOMPLETE`` (missing indices
or a report declares more than is on disk), ``DAMAGED`` (truncated or
duplicate chunks) or ``UNKNOWN`` (nothing to compare against).  Nothing
beyond DIR as given, chunk names and report file names is ever printed.
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
FINAL_SUMMARY_GLOB = "final_summary*.txt"
SEQUENCING_SUMMARY_GLOB = "sequencing_summary*.txt"
SIBLINGS = {"bam_pass": "bam_fail", "bam_fail": "bam_pass"}

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
    inventory.add_argument("--recursive", action="store_true", help="also scan subdirectories")
    inventory.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="a MinKNOW final_summary*.txt or sequencing_summary*.txt to compare against (auto-detected otherwise)",
    )
    common(inventory)
    inventory.set_defaults(handler=inventory_command)


# -- chunks ---------------------------------------------------------------------------
@dataclass
class Entry:
    """One ``*.bam`` file under DIR; ``name`` is relative to DIR."""

    name: str
    path: Path
    index: int | None
    size: int
    eof_marker: bool = False
    quickcheck: bool = False
    header_ok: bool = False
    aligned: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    sha256: str | None = None
    records: int | None = None
    records_kind: str | None = None  # "exact" | "estimate"
    cached: bool = False

    @property
    def truncated(self) -> bool:
        return not (self.eof_marker and self.quickcheck and self.header_ok)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "bytes": self.size,
            "eof_marker": self.eof_marker,
            "quickcheck": self.quickcheck,
            "header_ok": self.header_ok,
            "truncated": self.truncated,
            "aligned": self.aligned,
            "sha256": self.sha256,
            "records": self.records,
            "records_kind": self.records_kind,
            "hash_cached": self.cached,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class Skipped:
    name: str
    kind: str


def _refuse_dir(kind: str, shown: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-005",
        cause=f"DIR ({shown}) is {kind}",
        fix="Pass the directory of MinKNOW chunks (realpath), or fix its permissions",
        data={"argument": 1, "kind": kind},
    )


def list_chunks(directory: Path, *, recursive: bool) -> tuple[list[Entry], list[Skipped]]:
    """Regular readable ``*.bam`` files under DIR; symlinks and the like are listed as skipped."""

    entries: list[Entry] = []
    skipped: list[Skipped] = []

    def visit(folder: Path, prefix: str) -> None:
        try:
            names = sorted(os.listdir(folder))
        except OSError as exc:
            raise _refuse_dir("unreadable", prefix or directory.name or str(directory)) from exc
        for name in names:
            if name.startswith("."):
                continue
            shown = f"{prefix}{name}"
            path = folder / name
            try:
                info = os.lstat(path)
            except OSError:
                skipped.append(Skipped(shown, "unreadable"))
                continue
            if stat.S_ISDIR(info.st_mode):
                if recursive:
                    visit(path, f"{shown}/")
                continue
            if not name.endswith(".bam"):
                continue
            if stat.S_ISLNK(info.st_mode):
                skipped.append(Skipped(shown, "a symlink"))
            elif not stat.S_ISREG(info.st_mode):
                skipped.append(Skipped(shown, "not a regular file"))
            elif not os.access(path, os.R_OK):
                skipped.append(Skipped(shown, "unreadable"))
            else:
                entries.append(Entry(name=shown, path=path, index=chunk_index(name), size=info.st_size))

    visit(directory, "")
    entries.sort(key=lambda entry: _order_key(entry.path))
    return entries, skipped


def _digest_prefix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_PREFIX]


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


def estimate_records(path: Path, size: int) -> tuple[int, str]:
    """Records in one BAM: exact when the whole file is read, else extrapolated by compressed size.

    The rate is records wholly before a BGZF block boundary over that boundary's
    compressed offset, so a record straddling blocks never skews it.
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
            return seen, "exact"
    if block_start <= 0:
        return count_records(path), "exact"
    return max(seen, round(before_block * size / block_start)), "estimate"


def inspect_entries(
    entries: Sequence[Entry], store: LabStore, *, progress: Callable[[str], None] | None = None
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
        cached = None
        try:
            cached = cache.get(store.hash_cache_key(entry.path))
        except OSError:
            cached = None
        if cached and isinstance(cached.get("sha256"), str) and isinstance(cached.get("records"), int):
            entry.sha256 = cached["sha256"]
            entry.records = cached["records"]
            entry.records_kind = "exact"
            entry.cached = True
            continue
        if progress is not None:
            progress(f"inventory  hashing chunk {number}/{len(entries)}")
        try:
            entry.sha256 = _sha256_file(entry.path)
        except OSError:
            entry.sha256 = None
        if entry.truncated:
            continue
        try:
            entry.records, entry.records_kind = estimate_records(entry.path, entry.size)
        except Exception:  # noqa: BLE001 - an unreadable body is reported, not raised
            entry.records, entry.records_kind = None, None


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


def duplicates(entries: Sequence[Entry]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    by_hash: dict[str, Entry] = {}
    by_index: dict[int, Entry] = {}
    for entry in entries:
        if entry.sha256:
            if entry.sha256 in by_hash:
                found.append({"kind": "same_content", "names": [by_hash[entry.sha256].name, entry.name],
                              "sha256_12": entry.sha256[:DIGEST_PREFIX]})
            else:
                by_hash[entry.sha256] = entry
        if entry.index is not None:
            other = by_index.get(entry.index)
            if other is not None and other.sha256 != entry.sha256:
                found.append({"kind": "same_index", "names": [other.name, entry.name], "index": entry.index})
            by_index.setdefault(entry.index, entry)
    return found


# -- MinKNOW reports ------------------------------------------------------------------------
def _is_key_value(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            first = stream.readline()
    except OSError:
        return False
    return "=" in first and "\t" not in first


def parse_final_summary(path: Path) -> dict[str, Any]:
    """``key=value`` lines: BAM-file counts only; identifiers are reported as present, never as values."""

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
    declared = None if final is None else final + (fallback or 0)
    return {
        "kind": "final_summary",
        "declared_bam_files": declared,
        "declared_bam_files_in_fallback": fallback,
        "basecalling_enabled": values.get("basecalling_enabled"),
        "sample_id_present": bool(values.get("sample_id")),
        "protocol_run_id_present": bool(values.get("protocol_run_id")),
        "keys": len(values),
    }


def parse_sequencing_summary(path: Path) -> dict[str, Any]:
    """One row per read: total and pass read counts, and the pass rows' BAM file names when listed."""

    total = passed = 0
    pass_files: set[str] = set()
    all_files: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        header = stream.readline().rstrip("\n").split("\t")
        columns = {name: position for position, name in enumerate(header)}
        file_column = columns.get("filename_bam")
        pass_column = columns.get("passes_filtering")
        for line in stream:
            if not line.strip():
                continue
            cells = line.rstrip("\n").split("\t")
            total += 1
            is_pass = pass_column is None or (len(cells) > pass_column and cells[pass_column].upper() == "TRUE")
            name = None
            if file_column is not None and len(cells) > file_column and cells[file_column]:
                name = cells[file_column]
                all_files.add(name)
            if is_pass:
                passed += 1
                if name:
                    pass_files.add(name)
    return {
        "kind": "sequencing_summary",
        "declared_reads": total,
        "declared_pass_reads": passed if pass_column is not None else None,
        "declared_bam_files": len(all_files) if file_column is not None else None,
        "declared_pass_bam_files": sorted(pass_files) if file_column is not None else None,
    }


def find_reports(directory: Path, explicit: Path | None) -> list[tuple[Path, str]]:
    """``--report`` as given, else ``final_summary*``/``sequencing_summary*`` in DIR, then its parent."""

    if explicit is not None:
        return [(Path(os.path.abspath(explicit.expanduser())), "--report")]
    found: list[tuple[Path, str]] = []
    for folder, where in ((directory, "DIR"), (directory.parent, "parent directory")):
        for pattern in (FINAL_SUMMARY_GLOB, SEQUENCING_SUMMARY_GLOB):
            matches = sorted(path for path in folder.glob(pattern) if path.is_file())
            if matches:
                found.append((matches[0], where))
        if found:
            break
    return found


def read_report(path: Path, where: str) -> dict[str, Any]:
    """Parse one report by name, else by shape; an unreadable report is reported, not raised."""

    name = path.name
    try:
        if name.startswith("final_summary") or (not name.startswith("sequencing_summary") and _is_key_value(path)):
            parsed = parse_final_summary(path)
        else:
            parsed = parse_sequencing_summary(path)
    except OSError as exc:
        parsed = {"kind": "unreadable", "error": type(exc).__name__}
    return {"name": name, "where": where, **parsed}


def sibling_chunks(directory: Path) -> dict[str, Any] | None:
    """MinKNOW writes ``bam_pass/`` and ``bam_fail/`` side by side; count the other one's files."""

    other = SIBLINGS.get(directory.name)
    if other is None:
        return None
    sibling = directory.parent / other
    if not sibling.is_dir():
        return None
    try:
        count = sum(1 for name in os.listdir(sibling) if name.endswith(".bam") and not name.startswith("."))
    except OSError:
        return None
    return {"name": other, "bam_files": count}


# -- verdict --------------------------------------------------------------------------------
def compare_reports(
    reports: Sequence[dict[str, Any]], entries: Sequence[Entry], sibling: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Each report's declared counts against the disk; ``agrees`` is None when nothing is comparable."""

    on_disk_names = {entry.name for entry in entries}
    on_disk_files = len(entries) + (sibling["bam_files"] if sibling else 0)
    results: list[dict[str, Any]] = []
    for report in reports:
        comparison: dict[str, Any] = {"name": report["name"], "kind": report["kind"], "agrees": None, "reasons": []}
        if report["kind"] == "final_summary" and report.get("declared_bam_files") is not None:
            declared = report["declared_bam_files"]
            comparison["declared_bam_files"] = declared
            comparison["on_disk_bam_files"] = on_disk_files
            comparison["agrees"] = declared == on_disk_files
            if declared != on_disk_files:
                comparison["reasons"].append(
                    f"{report['name']} declares {declared} BAM file(s); {on_disk_files} on disk"
                    + (f" (DIR plus {sibling['name']})" if sibling else "")
                )
        elif report["kind"] == "sequencing_summary":
            declared_names = report.get("declared_pass_bam_files")
            if declared_names is not None:
                missing = sorted(name for name in declared_names if name not in on_disk_names)
                extra = sorted(name for name in on_disk_names if name not in set(declared_names))
                comparison["declared_pass_bam_files"] = len(declared_names)
                comparison["missing_on_disk"] = missing
                comparison["not_declared"] = extra
                comparison["agrees"] = not missing
                if missing:
                    comparison["reasons"].append(
                        f"{report['name']} lists {len(missing)} pass chunk(s) not on disk"
                    )
            comparison["declared_reads"] = report.get("declared_reads")
            comparison["declared_pass_reads"] = report.get("declared_pass_reads")
        results.append(comparison)
    return results


def verdict_for(
    entries: Sequence[Entry], found_duplicates: Sequence[dict[str, Any]],
    gaps: Sequence[tuple[int, int]], comparisons: Sequence[dict[str, Any]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    truncated = [entry.name for entry in entries if entry.truncated]
    if truncated:
        reasons.append(f"{len(truncated)} truncated chunk(s)")
    if found_duplicates:
        reasons.append(f"{len(found_duplicates)} duplicate(s)")
    if reasons:
        return VERDICT_DAMAGED, reasons
    missing = sum(b - a + 1 for a, b in gaps)
    if missing:
        reasons.append(f"missing {missing} index(es): {format_ranges(gaps)}")
    for comparison in comparisons:
        reasons.extend(comparison["reasons"])
    if reasons:
        return VERDICT_INCOMPLETE, reasons
    if any(comparison["agrees"] for comparison in comparisons):
        return VERDICT_COMPLETE, ["a MinKNOW report agrees with the chunks on disk"]
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


def inventory_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    given = str(args.directory)
    directory = Path(os.path.abspath(Path(given).expanduser()))
    shown = Path(given).name or given
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise _refuse_dir("missing", shown) from None
    except OSError:
        raise _refuse_dir("unreadable", shown) from None
    if stat.S_ISLNK(info.st_mode):
        raise _refuse_dir("a symlink", shown)
    if not stat.S_ISDIR(info.st_mode):
        raise _refuse_dir("not a directory", shown)

    entries, skipped = list_chunks(directory, recursive=bool(args.recursive))
    if not entries:
        raise LabProblem(
            "TBX-LAB-031",
            cause=f"DIR ({shown}) holds no readable .bam chunk" + (" (searched recursively)" if args.recursive else ""),
            fix="Point at the MinKNOW bam_pass directory, or add --recursive for a run folder",
            data={"dir": given, "skipped": [{"name": s.name, "kind": s.kind} for s in skipped]},
        )

    def progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    inspect_entries(entries, store, progress=progress)
    found_duplicates = duplicates(entries)
    gaps = missing_ranges(entry.index for entry in entries if entry.index is not None)
    unindexed = [entry.name for entry in entries if entry.index is None]
    sibling = sibling_chunks(directory)
    reports = [read_report(path, where) for path, where in find_reports(directory, args.report)]
    comparisons = compare_reports(reports, entries, sibling)
    verdict, reasons = verdict_for(entries, found_duplicates, gaps, comparisons)

    total_bytes = sum(entry.size for entry in entries)
    counted = [entry for entry in entries if entry.records is not None]
    estimated = any(entry.records_kind == "estimate" for entry in counted)
    total_records = sum(entry.records for entry in counted) if counted else None
    provenance = _merge_provenance(entries)
    indexes = sorted(entry.index for entry in entries if entry.index is not None)

    data: dict[str, Any] = {
        "root": str(store.root),
        "dir": given,
        "recursive": bool(args.recursive),
        "verdict": verdict,
        "reasons": reasons,
        "chunks": len(entries),
        "total_bytes": total_bytes,
        "index_min": indexes[0] if indexes else None,
        "index_max": indexes[-1] if indexes else None,
        "missing_indexes": [list(gap) for gap in gaps],
        "missing_index_count": sum(b - a + 1 for a, b in gaps),
        "unindexed": unindexed,
        "truncated": [entry.name for entry in entries if entry.truncated],
        "duplicates": found_duplicates,
        "records": {
            "total": total_records,
            "kind": None if total_records is None else ("estimate" if estimated else "exact"),
            "counted_chunks": len(counted),
            "sample_records": SAMPLE_RECORDS,
        },
        "provenance": provenance,
        "hash_cached_chunks": sum(entry.cached for entry in entries),
        "sibling": sibling,
        "reports": reports,
        "comparisons": comparisons,
        "skipped": [{"name": s.name, "kind": s.kind} for s in skipped],
        "entries": [entry.public() for entry in entries],
    }

    lines = [f"ROOT  {store.root}", f"DIR   {given}"]
    first, last = entries[0].name, entries[-1].name
    lines.append(
        f"CHUNKS      {len(entries)}  ({first}" + (f" … {last}" if len(entries) > 1 else "") + f")  {total_bytes:,} bytes"
    )
    if indexes:
        coverage = f"{indexes[0]}-{indexes[-1]}"
        lines.append(
            f"INDICES     {coverage} "
            + ("contiguous" if not gaps else f"missing {format_ranges(gaps)} ({data['missing_index_count']} index(es))")
            + (f"; {len(unindexed)} file(s) without a _<n>.bam index" if unindexed else "")
        )
    else:
        lines.append("INDICES     none of the files carries a _<n>.bam index")
    truncated = data["truncated"]
    lines.append("TRUNCATED   " + (", ".join(truncated) if truncated else "none"))
    lines.append(
        "DUPLICATES  "
        + ("; ".join(
            f"{' = '.join(d['names'])} ({'same content, sha256 ' + d['sha256_12'] if d['kind'] == 'same_content' else 'same index ' + str(d['index']) + ', different content'})"
            for d in found_duplicates
        ) if found_duplicates else "none")
    )
    if total_records is None:
        lines.append("READS       unavailable")
    else:
        label = f"~{total_records:,} (estimate from the first {SAMPLE_RECORDS:,} records of each chunk" if estimated else f"{total_records:,} (exact"
        exact = sum(entry.records_kind == "exact" for entry in counted)
        if estimated and exact:
            label += f"; {exact} chunk(s) exact"
        lines.append(f"READS       {label})")
    lines.append(
        "PROVENANCE  basecall " + (", ".join(provenance["basecall_models"]) or "undeclared")
        + "; modbase " + (", ".join(provenance["modbase_models"]) or "undeclared")
        + "; programs " + (", ".join(f"{p['name']} {p['version'] or '?'}" for p in provenance["programs"]) or "none")
    )
    lines.append(f"            run id {_fmt_ids(provenance['run_id'])}; sample id {_fmt_ids(provenance['sample_id'])}")
    if data["hash_cached_chunks"]:
        lines.append(f"HASHES      {data['hash_cached_chunks']} of {len(entries)} chunk(s) from the Lab hash cache (read-only)")
    if sibling:
        lines.append(f"SIBLING     {sibling['name']}/: {sibling['bam_files']} .bam file(s) (names only)")
    if skipped:
        lines.append("SKIPPED     " + ", ".join(f"{s.name} ({s.kind})" for s in skipped) + "; lab run refuses these")
    if not reports:
        lines.append("REPORT      none found (final_summary*.txt or sequencing_summary*.txt in DIR or its parent; or --report PATH)")
    for report, comparison in zip(reports, comparisons):
        if report["kind"] == "final_summary":
            detail = (
                f"declares {report['declared_bam_files']} BAM file(s); {comparison.get('on_disk_bam_files')} on disk"
                if report.get("declared_bam_files") is not None
                else "no bam_files_in_final_dest count"
            )
        elif report["kind"] == "sequencing_summary":
            detail = f"declares {report['declared_reads']:,} read(s)"
            if report.get("declared_pass_reads") is not None:
                detail += f", {report['declared_pass_reads']:,} pass"
            if report.get("declared_pass_bam_files") is not None:
                detail += f", {len(report['declared_pass_bam_files'])} pass chunk(s)"
                if comparison.get("missing_on_disk"):
                    detail += f"; {len(comparison['missing_on_disk'])} not on disk"
                elif comparison.get("not_declared"):
                    detail += f"; {len(comparison['not_declared'])} on disk not listed"
                else:
                    detail += "; names match"
        else:
            detail = "unreadable"
        lines.append(f"REPORT      {report['name']} ({report['where']}): {detail}")
    lines.append(f"VERDICT     {verdict}: " + "; ".join(reasons))
    lines.append("")
    lines.append(_entries_table(entries))

    summary = f"inventory {verdict}: {len(entries)} chunk(s), " + "; ".join(reasons)
    if verdict in {VERDICT_COMPLETE, VERDICT_UNKNOWN}:
        payload = _local_envelope(_result("lab inventory", "ok", summary, data=data))
        return EXIT_OK, _with_human(payload, lines, replace_data=True)
    payload = _local_envelope(_result("lab inventory", "partial_failure", summary, data=data))
    return EXIT_PARTIAL, _with_human(payload, lines, replace_data=True)


def _entries_table(entries: Sequence[Entry]) -> str:
    header = ("INDEX", "NAME", "BYTES", "EOF", "QUICKCHECK", "SHA256", "RECORDS")
    rows = [
        (
            "-" if entry.index is None else str(entry.index),
            entry.name,
            f"{entry.size:,}",
            "yes" if entry.eof_marker else "NO",
            "ok" if entry.quickcheck else "FAIL",
            (entry.sha256 or "")[:DIGEST_PREFIX] or "-",
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
    "SAMPLE_BYTES",
    "SAMPLE_RECORDS",
    "VERDICT_COMPLETE",
    "VERDICT_DAMAGED",
    "VERDICT_INCOMPLETE",
    "VERDICT_UNKNOWN",
    "add_inventory_parser",
    "compare_reports",
    "duplicates",
    "estimate_records",
    "find_reports",
    "header_provenance",
    "inspect_entries",
    "inventory_command",
    "list_chunks",
    "missing_ranges",
    "parse_final_summary",
    "parse_sequencing_summary",
    "verdict_for",
]
