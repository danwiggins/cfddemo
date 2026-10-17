"""Deterministic, private ONT patient-source mixtures; never known tumour fractions.

Run from the repository with ``python -m scripts.validation.mix_public_bams``.
Inputs must already be coordinate sorted. Selection is without replacement,
using a disk-backed index, and publication is exclusive and atomic on Linux/macOS.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from fractions import Fraction
from pathlib import Path
from typing import Any

import pysam

from traceback_lab.algorithms._records import eligible_record
from traceback_lab.read_summary import FLAG_EXCLUDE, MIN_MAPQ
from traceback_runner.filesystem import rename_directory_exclusive_at

FRACTIONS = ("0.01", "0.03", "0.10")
CANONICAL = re.compile(r"^chr([1-9]|1[0-9]|2[0-2]|X|Y)$")
SCHEMA = "traceback.validation.patient-source-mixture.v1"


class MixtureError(ValueError):
    """Invalid inputs, without private record identifiers in the message."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rank(seed: int, source: int, name: str) -> bytes:
    return hashlib.sha256(json.dumps([SCHEMA, seed, source, name], separators=(",", ":")).encode()).digest()


def _budget(stage: Path, maximum: int) -> None:
    if sum(p.stat().st_size for p in stage.iterdir() if p.is_file()) > maximum:
        raise MixtureError("staging disk budget exceeded; no mixture published")


def _header(path: Path) -> dict[str, Any]:
    with pysam.AlignmentFile(str(path), "rb") as bam:
        bam.check_truncation()
        header = bam.header.to_dict()
    if header.get("HD", {}).get("SO") != "coordinate":
        raise MixtureError("source header must declare coordinate sorting")
    groups = header.get("RG", [])
    if not groups or any(str(g.get("PL", "")).upper() != "ONT" for g in groups):
        raise MixtureError("every source read group must declare ONT")
    for kind in ("RG", "PG"):
        ids = [row.get("ID") for row in header.get(kind, [])]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise MixtureError("source header has missing or duplicate group/program IDs")
    sq = header.get("SQ", [])
    if not sq or len({r["SN"] for r in sq}) != len(sq):
        raise MixtureError("source reference dictionary is missing or duplicated")
    return header


def _reference(headers: list[dict[str, Any]], fasta: Path | None) -> dict[str, Any]:
    left, right = (h["SQ"] for h in headers)
    if [(r["SN"], r["LN"]) for r in left] != [(r["SN"], r["LN"]) for r in right]:
        raise MixtureError("ordered reference names/lengths differ")
    if any(a.get("M5") and b.get("M5") and a["M5"].lower() != b["M5"].lower()
           for a, b in zip(left, right, strict=True)):
        raise MixtureError("reference M5 values differ")
    evidence: dict[str, Any] = {
        "ordered_names_lengths_match": True,
        "both_sources_have_all_m5": all(row.get("M5") for h in headers for row in h["SQ"]),
        "fasta_sha256": None,
        "limitation": "Matching headers do not independently verify the reference used to align either source.",
    }
    if fasta is not None:
        byte_digest = hashlib.sha256()
        observed = []
        name, length, md5 = None, 0, hashlib.md5(usedforsecurity=False)
        line_start = True
        with fasta.open("rb") as stream:
            for line in iter(lambda: stream.readline(65536), b""):
                byte_digest.update(line)
                if line_start and line.startswith(b">"):
                    # Sequence lines can be arbitrarily long; headers are bounded.
                    if len(line) == 65536 and not line.endswith(b"\n"):
                        raise MixtureError("reference FASTA header exceeds 64 KiB")
                    if name is not None:
                        observed.append((name, length, md5.hexdigest()))
                    if len(observed) >= len(left):
                        raise MixtureError("reference FASTA dictionary differs")
                    fields = line[1:].split()
                    if not fields:
                        raise MixtureError("invalid reference FASTA header")
                    name = fields[0].decode("ascii")
                    length, md5 = 0, hashlib.md5(usedforsecurity=False)
                else:
                    bases = b"".join(line.split()).upper()
                    if bases and name is None:
                        raise MixtureError("invalid reference FASTA")
                    length += len(bases)
                    md5.update(bases)
                line_start = line.endswith(b"\n")
        if name is not None:
            observed.append((name, length, md5.hexdigest()))
        if [(n, size) for n, size, _ in observed] != [(r["SN"], r["LN"]) for r in left]:
            raise MixtureError("reference FASTA dictionary differs")
        for header in headers:
            for row, (_, _, digest) in zip(header["SQ"], observed, strict=True):
                if row.get("M5") and row["M5"].lower() != digest:
                    raise MixtureError("reference FASTA disagrees with source M5")
        evidence.update(fasta_sha256=byte_digest.hexdigest(), fasta_dictionary_and_available_m5_match=True)
    return evidence


def _merged_header(headers: list[dict[str, Any]]) -> dict[str, Any]:
    output = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": copy.deepcopy(headers[0]["SQ"]), "RG": [], "PG": [], "CO": []}
    for source, header in enumerate(headers):
        prefix = f"s{source}."
        for kind, links in (("RG", ("PG",)), ("PG", ("PP",))):
            for item in header.get(kind, []):
                row = dict(item)
                row["ID"] = prefix + row["ID"]
                for link in links:
                    if link in row:
                        if row[link] not in {p["ID"] for p in header.get("PG", [])}:
                            raise MixtureError("source header has an unresolved program reference")
                        row[link] = prefix + row[link]
                output[kind].append(row)
        output["CO"].extend(f"source {source}: {comment}" for comment in header.get("CO", []))
    output["CO"].append("Patient-source read mixture; source 1 fraction is NOT known tumour fraction.")
    return output


def _scan(path: Path, header: dict[str, Any], contigs: set[str], source: int,
          seed: int, db: sqlite3.Connection, stage: Path, maximum: int) -> dict[str, int]:
    total, count = 0, 0
    last = (-1, -1)
    unmapped_tail = False
    with pysam.AlignmentFile(str(path), "rb") as bam:
        for ordinal, record in enumerate(bam.fetch(until_eof=True)):
            total += 1
            if record.reference_id < 0:
                unmapped_tail = True
            else:
                position = (record.reference_id, record.reference_start)
                if unmapped_tail or position < last:
                    raise MixtureError("source is not coordinate sorted or has records after its unmapped tail")
                last = position
            if not eligible_record(record, contigs):
                continue
            if record.is_paired:
                raise MixtureError("paired eligible records are unsupported in this ONT molecule mixture")
            if not record.query_name or record.reference_end is None or record.reference_end <= record.reference_start:
                raise MixtureError("eligible record lacks a name or positive aligned span")
            if record.has_tag("ZQ") or record.has_tag("ZS"):
                raise MixtureError("eligible record already uses reserved provenance tags ZQ/ZS")
            for tag in ("RG", "PG"):
                if tag == "RG" and not record.has_tag(tag):
                    raise MixtureError("eligible record lacks an ONT read-group link")
                if record.has_tag(tag) and record.get_tag(tag) not in {r["ID"] for r in header.get(tag, [])}:
                    raise MixtureError("eligible record has an unresolved group/program link")
            try:
                db.execute("INSERT INTO reads VALUES (?, ?, ?, ?)", (source, record.query_name, rank(seed, source, record.query_name), ordinal))
            except sqlite3.IntegrityError as exc:
                raise MixtureError("duplicate eligible read name within a source") from exc
            count += 1
            if count % 4096 == 0:
                db.commit()
                _budget(stage, maximum)
    db.commit()
    return {"records": total, "unique_eligible_reads": count}


def _selected(path: Path, contigs: set[str], source: int, seed: int, boundary: tuple[bytes, int]):
    with pysam.AlignmentFile(str(path), "rb") as bam:
        for ordinal, record in enumerate(bam.fetch(until_eof=True)):
            if eligible_record(record, contigs) and (rank(seed, source, record.query_name), ordinal) <= boundary:
                original = record.query_name
                record.query_name = f"source{source}.record{ordinal}"
                record.set_tag("ZQ", original, value_type="Z")
                record.set_tag("ZS", source, value_type="i")
                for tag in ("RG", "PG"):
                    if record.has_tag(tag):
                        record.set_tag(tag, f"s{source}." + record.get_tag(tag), value_type="Z")
                yield record


def _write(path: Path, sources: list[Path], header: dict[str, Any], contigs: set[str],
           seed: int, boundaries: list[tuple[bytes, int]], stage: Path, maximum: int) -> list[int]:
    counts = [0, 0]
    with ExitStack() as stack:
        streams = [stack.enter_context(closing(_selected(p, contigs, i, seed, boundaries[i]))) for i, p in enumerate(sources)]
        heads = [next(s, None) for s in streams]
        writer = stack.enter_context(pysam.AlignmentFile(str(path), "wb", header=header))
        while any(r is not None for r in heads):
            source = min((i for i in (0, 1) if heads[i] is not None),
                         key=lambda i: (heads[i].reference_id, heads[i].reference_start, i))
            writer.write(heads[source])
            counts[source] += 1
            heads[source] = next(streams[source], None)
            if sum(counts) % 4096 == 0:
                _budget(stage, maximum)
    pysam.index("-c", str(path))
    _budget(stage, maximum)
    return counts


def mix_sources(background: Path, patient: Path, output: Path, *, total: int, seed: int = 1729,
                reference_fasta: Path | None = None, max_work_bytes: int = 20 * 1024**3) -> dict[str, Any]:
    """Publish three source-fraction mixtures without replacement or overwrite."""
    if isinstance(total, bool) or not isinstance(total, int) or total < 100 or total % 100:
        raise MixtureError("total must be a positive multiple of 100 for exact 1/3/10 percent counts")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise MixtureError("seed must be an integer")
    if max_work_bytes < 1024 * 1024:
        raise MixtureError("staging disk budget must be at least 1 MiB")
    sources = [Path(background).resolve(strict=True), Path(patient).resolve(strict=True)]
    if any(not path.is_file() for path in sources):
        raise MixtureError("sources must be regular BAM files")
    if reference_fasta is not None:
        reference_fasta = Path(reference_fasta).resolve(strict=True)
        if not reference_fasta.is_file():
            raise MixtureError("reference must be a regular FASTA file")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise MixtureError("output already exists; refusing overwrite")
    if not output.parent.is_dir():
        raise MixtureError("output parent must already exist")
    hashes = [sha256(p) for p in sources]
    if hashes[0] == hashes[1]:
        raise MixtureError("sources have identical content digests")
    headers = [_header(p) for p in sources]
    reference = _reference(headers, reference_fasta)
    contigs = {r["SN"] for r in headers[0]["SQ"] if CANONICAL.fullmatch(r["SN"])}
    if not contigs:
        raise MixtureError("reference has no chr1-22/X/Y nuclear contigs")
    header = _merged_header(headers)
    required = min(max_work_bytes, sum(p.stat().st_size for p in sources) * 4 + 16 * 1024**2)
    if shutil.disk_usage(output.parent).free < required:
        raise MixtureError("insufficient free disk for staging budget")
    stage = Path(tempfile.mkdtemp(prefix=".mixture-", dir=output.parent))
    published = False
    try:
        manifest: dict[str, Any] = {
            "schema": SCHEMA, "seed": seed, "total_reads_per_mixture": total,
            "fraction_semantics": "patient-source eligible-read fraction, NOT true tumour fraction",
            "true_tumour_fraction": None, "reference": reference,
            "eligibility": {"excluded_flags": FLAG_EXCLUDE, "minimum_mapq": MIN_MAPQ,
                            "contigs": [r["SN"] for r in headers[0]["SQ"] if r["SN"] in contigs],
                            "policy": "D21 nuclear subset matching read-summary canonical default; excludes chrM; no toy-reference fallback; paired eligible records refused"},
            "selection": "SHA256(schema,seed,source,qname) ascending; original record ordinal breaks ties; nested per source; no replacement",
            "provenance_tags": {"ZQ": "original private qname", "ZS": "source index: 0 background, 1 patient"},
            "header_links": "RG/PG IDs and links prefixed s0./s1.; all other source tags unchanged",
            "sources": [], "mixtures": [], "max_work_bytes": max_work_bytes,
            "software": {"pysam": pysam.__version__, "samtools": pysam.__samtools_version__,
                         "utility_sha256": sha256(Path(__file__))},
        }
        database = stage / "selection.sqlite"
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA cache_size=-8192")
            db.execute("PRAGMA temp_store=FILE")
            db.execute(f"PRAGMA max_page_count={max_work_bytes // 4096}")
            db.execute("CREATE TABLE reads (source INTEGER, name TEXT, score BLOB, ordinal INTEGER, PRIMARY KEY(source,name))")
            db.execute("CREATE INDEX ranking ON reads(source,score,ordinal)")
            for i, path in enumerate(sources):
                counts = _scan(path, headers[i], contigs, i, seed, db, stage, max_work_bytes)
                manifest["sources"].append({"index": i, "sha256": hashes[i], "header": headers[i], **counts})
            for fraction in FRACTIONS:
                k = int(Fraction(fraction) * total)
                desired = [total - k, k]
                boundaries = []
                for i, count in enumerate(desired):
                    if count > manifest["sources"][i]["unique_eligible_reads"]:
                        raise MixtureError("requested mixture exceeds unique eligible source reads")
                    boundaries.append(db.execute("SELECT score,ordinal FROM reads WHERE source=? ORDER BY score,ordinal LIMIT 1 OFFSET ?", (i, count - 1)).fetchone())
                filename = f"patient-source-{fraction}.bam"
                counts = _write(stage / filename, sources, header, contigs, seed, boundaries, stage, max_work_bytes)
                if counts != desired:
                    raise MixtureError("source changed or selection counts disagree")
                manifest["mixtures"].append({"file": filename, "sha256": sha256(stage / filename),
                                             "index_sha256": sha256(stage / (filename + ".csi")),
                                             "selected_counts": counts, "target_q": fraction,
                                             "actual_q": {"numerator": counts[1], "denominator": sum(counts)}})
        database.unlink()
        if [sha256(p) for p in sources] != hashes:
            raise MixtureError("source changed during mixture construction")
        if reference_fasta is not None and sha256(reference_fasta) != reference["fasta_sha256"]:
            raise MixtureError("reference changed during mixture construction")
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _budget(stage, max_work_bytes)
        for path in stage.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        stage_descriptor = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(stage_descriptor)
        finally:
            os.close(stage_descriptor)
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            rename_directory_exclusive_at(descriptor, stage.name, output.name)
            published = True
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return manifest
    finally:
        if not published:
            shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("background", type=Path)
    parser.add_argument("patient", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--total", type=int, required=True)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--reference-fasta", type=Path)
    parser.add_argument("--max-work-bytes", type=int, default=20 * 1024**3)
    args = parser.parse_args(argv)
    verbosity = pysam.set_verbosity(0)
    try:
        mix_sources(args.background, args.patient, args.output, total=args.total, seed=args.seed,
                    reference_fasta=args.reference_fasta, max_work_bytes=args.max_work_bytes)
    except (OSError, ValueError, sqlite3.Error, pysam.SamtoolsError):
        # HTSlib errors may embed paths or names. Details stay local to API callers.
        print("Mixture construction refused or failed; no partial output was published. Check inputs, available reads, reference/platform and disk budget.")
        return 2
    finally:
        pysam.set_verbosity(verbosity)
    print("Published three patient-source mixtures; these are not known tumour fractions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
