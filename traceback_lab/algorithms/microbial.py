"""Exploratory Kraken2/Bracken profile of human-unmapped ONT reads.

One primary QC-passing, non-duplicate read is one denominator unit. Only
unmapped reads at least the registered Bracken read length are submitted,
clipped to exactly that length. Kraken's report supplies lineage; Bracken's
species estimate supplies candidate microbial counts. Neither is a diagnosis.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam

from traceback_runner.serialization import canonical_json_bytes

from ..assets import AssetError, _check_contained
from ..microbial_db import MicrobialDbError, inspect_database
from ..registry import AlgorithmOutput, InputContext, Ready, Skip, ToolPin
from ._base import Base, metric

MAX_TAXA = 256
MAX_REPORT_ROWS = 100_000
MAX_CANDIDATE_FASTA_BYTES = 512 << 20
_BRACKEN_COLUMNS = ("name", "taxonomy_id", "taxonomy_lvl", "kraken_assigned_reads",
                    "added_reads", "new_est_reads", "fraction_total_reads")
_ROOTS = {2: "bacteria", 2157: "archaea", 10239: "viruses", 4751: "fungi"}
_TAXON = re.compile(r"[1-9][0-9]{0,11}\Z")


def _tool(name: str) -> tuple[str, str] | None:
    path = shutil.which(name)
    if path is None:
        return None
    try:
        launcher = Path(path).resolve(strict=True)
        components = [launcher]
        if name == "kraken2":
            components.extend((launcher.with_name("classify"), launcher.with_name("kraken2lib.pm")))
        elif name == "bracken":
            python = shutil.which("python")
            if python is None:
                return None
            components.extend((launcher.parent / "src" / "est_abundance.py", Path(python).resolve(strict=True)))
        hashes = {}
        for component in components:
            digest = hashlib.sha256()
            with component.open("rb") as stream:
                while block := stream.read(1 << 20):
                    digest.update(block)
            hashes[component.name] = digest.hexdigest()
    except OSError:
        return None
    return path, hashlib.sha256(canonical_json_bytes(hashes)).hexdigest()


def _report_lineages(path: Path, candidates: int) -> tuple[dict[int, str], int, int, bool]:
    lineages: dict[int, str] = {}
    stack: list[tuple[int, str | None]] = []
    root_count = unclassified = 0
    species_present = False
    seen: set[int] = set()
    with path.open("rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= MAX_REPORT_ROWS:
                raise RuntimeError("Kraken2 report exceeds the row limit")
            fields = line.rstrip("\n").split("\t", 5)
            if len(fields) != 6:
                raise RuntimeError("Kraken2 report has malformed columns")
            try:
                clade = int(fields[1])
                taxid = int(fields[4])
            except ValueError as exc:
                raise RuntimeError("Kraken2 report has invalid counts or taxids") from exc
            if clade < 0 or taxid < 0 or taxid in seen:
                raise RuntimeError("Kraken2 report has duplicate or invalid taxids")
            seen.add(taxid)
            if taxid == 0:
                unclassified = clade
                continue
            if taxid == 1:
                root_count = clade
            raw_name = fields[5]
            indent = len(raw_name) - len(raw_name.lstrip(" "))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else None
            category = _ROOTS.get(taxid, parent)
            if category is not None:
                lineages[taxid] = category
                if fields[3] == "S" and clade > 0:
                    species_present = True
            stack.append((indent, category))
    if root_count + unclassified != candidates:
        raise RuntimeError("Kraken2 report does not account for submitted reads")
    return lineages, root_count, unclassified, species_present


def _bracken_species(path: Path, lineages: dict[int, str], denominator: int) -> tuple[list[dict[str, Any]], float, int]:
    rows: list[dict[str, Any]] = []
    total = 0.0
    accepted = 0
    seen: set[int] = set()
    with path.open("rt", encoding="utf-8") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != list(_BRACKEN_COLUMNS):
            raise RuntimeError("Bracken output has an invalid header")
        for index, row in enumerate(reader):
            if index >= MAX_REPORT_ROWS or None in row:
                raise RuntimeError("Bracken output exceeds its row or column limit")
            raw_id = row["taxonomy_id"]
            if not isinstance(raw_id, str) or not _TAXON.fullmatch(raw_id) or row["taxonomy_lvl"] != "S":
                raise RuntimeError("Bracken output has an invalid species")
            taxid = int(raw_id)
            if taxid in seen:
                raise RuntimeError("Bracken output repeats a species taxid")
            seen.add(taxid)
            try:
                estimated = float(row["new_est_reads"])
                assigned = int(row["kraken_assigned_reads"])
                added = int(row["added_reads"])
                fraction = float(row["fraction_total_reads"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Bracken output has an invalid estimate") from exc
            if (not math.isfinite(estimated) or estimated < 0 or assigned < 0 or added < 0
                    or not math.isfinite(fraction) or not 0 <= fraction <= 1):
                raise RuntimeError("Bracken output has an invalid estimate")
            category = lineages.get(taxid)
            if category is None or estimated == 0:
                continue
            name = row["name"]
            if not isinstance(name, str) or not 1 <= len(name) <= 160 or any(ord(char) < 32 for char in name):
                raise RuntimeError("Bracken output has an invalid taxon name")
            accepted += 1
            total += estimated
            rows.append({"taxid": taxid, "name": name, "category": category,
                         "estimated_reads": estimated, "rpm_primary": estimated * 1_000_000 / denominator})
    rows.sort(key=lambda row: (-row["estimated_reads"], row["taxid"]))
    return rows[:MAX_TAXA], total, max(0, accepted - MAX_TAXA)


class CandidateStagingExceeded(RuntimeError):
    """The complete candidate set needs a streamed classifier invocation."""


def _candidate_rows(bam: Path, read_length: int):
    """Yield anonymized FASTA records and final full-input counts."""
    denominator = candidate = too_short = missing_sequence = 0
    with pysam.AlignmentFile(bam, "rb", check_sq=False) as stream:
        for read in stream.fetch(until_eof=True):
            if read.is_secondary or read.is_supplementary or read.is_qcfail or read.is_duplicate:
                continue
            denominator += 1
            if not read.is_unmapped:
                continue
            sequence = read.query_sequence
            if sequence is None:
                missing_sequence += 1
                continue
            if len(sequence) < read_length:
                too_short += 1
                continue
            candidate += 1
            yield f">r{candidate}\n{sequence[:read_length]}\n".encode("ascii")
    return denominator, candidate, too_short, missing_sequence


def _write_candidates(bam: Path, fasta: Path, read_length: int) -> tuple[int, int, int, int]:
    rows = _candidate_rows(bam, read_length)
    staged_bytes = 0
    try:
        with fasta.open("wb") as out:
            while True:
                try:
                    row = next(rows)
                except StopIteration as done:
                    return done.value
                if staged_bytes + len(row) > MAX_CANDIDATE_FASTA_BYTES:
                    raise CandidateStagingExceeded("microbial candidates exceed the local FASTA staging budget")
                out.write(row)
                staged_bytes += len(row)
    except BaseException:
        try:
            fasta.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the original error; the worker scratch is still removed.
        raise
    finally:
        rows.close()


def _classify_streamed(bam: Path, read_length: int, kraken: str, database: Path,
                       report: Path) -> tuple[int, int, int, int]:
    """Send every candidate to Kraken without retaining read-level output."""
    argv = [kraken, "--db", str(database), "--threads", "1",
            "--report", str(report), "--output", "-", "/dev/fd/0"]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    result: list[tuple[int, int, int, int]] = []
    errors: list[BaseException] = []
    cancelled = threading.Event()

    def feed() -> None:
        rows = _candidate_rows(bam, read_length)
        try:
            assert process.stdin is not None
            while True:
                if cancelled.is_set():
                    return
                try:
                    row = next(rows)
                except StopIteration as done:
                    result.append(done.value)
                    break
                process.stdin.write(row)
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                rows.close()
            except BaseException as exc:
                errors.append(exc)
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BaseException as exc:
                    errors.append(exc)

    writer = threading.Thread(target=feed, name="microbial-kraken-feed", daemon=True)
    writer.start()
    try:
        process.wait(timeout=3600)
        writer.join(timeout=2)
        if writer.is_alive() or errors or process.returncode != 0 or not result:
            raise RuntimeError("Kraken2 streamed classification failed or input was incomplete")
        return result[0]
    except BaseException:
        cancelled.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        writer.join(timeout=2)
        if writer.is_alive() and process.stdin is not None:
            raw = getattr(process.stdin, "raw", None)
            try:
                if raw is not None:
                    raw.close()
            except OSError:
                pass
            writer.join(timeout=2)
        raise


@dataclass
class Microbial(Base):
    version: str = "5"
    family: str = "non-human"
    consumes: str = "full_bam"
    cost: str = "slow"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "input": "human-unmapped primary QC-passing non-duplicate reads",
        "denominator": "all primary QC-passing non-duplicate reads",
        "clipping": "first registered Bracken read-length bases",
        "bracken_level": "S", "bracken_threshold": 1,
        "microbial_roots": _ROOTS, "max_taxa": MAX_TAXA,
        "candidate_fasta_budget_bytes": MAX_CANDIDATE_FASTA_BYTES,
        "candidate_staging": "stream complete input to Kraken2 above FASTA budget",
    })

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        kraken = _tool("kraken2")
        bracken = _tool("bracken")
        return (ToolPin("kraken2", kraken[1] if kraken else "missing", "kraken2-mit"),
                ToolPin("bracken", bracken[1] if bracken else "missing", "bracken-gpl3"))

    @tools.setter
    def tools(self, value: tuple[ToolPin, ...]) -> None:
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        from ..store import LabStore

        try:
            return [inspect_database(LabStore(root), verify_bytes=False).sha256]
        except MicrobialDbError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        from ..store import LabStore

        if ctx.example:
            return Skip("example_reference", "microbial profiling requires a real registered database")
        if shutil.which("kraken2") is None or shutil.which("bracken") is None:
            return Skip("missing_toolchain", "Kraken2 and Bracken must be installed")
        if _tool("kraken2") is None or _tool("bracken") is None:
            return Skip("missing_toolchain", "Kraken2/Bracken launchers, upstream helpers, and Bracken's python interpreter are required")
        try:
            inspect_database(LabStore(ctx.root), verify_bytes=False)
        except MicrobialDbError:
            return Skip("missing_asset", "register a pinned Kraken2/Bracken database")
        if not (ctx.input_dir / "aligned.bam").is_file():
            return Skip("reference", "aligned BAM is unavailable")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        kraken, bracken = _tool("kraken2"), _tool("bracken")
        if kraken is None or bracken is None:
            raise RuntimeError("microbial tools changed after preflight")
        database = inspect_database(LabStore(ctx.root))
        planned_assets = ctx.extras.get("planned_asset_sha256s")
        planned_tools = ctx.extras.get("planned_tool_pins")
        current_tools = [tool.identity() for tool in self.tools]
        if planned_assets is not None and planned_assets != [database.sha256]:
            raise RuntimeError("microbial database changed after execution planning")
        if planned_tools is not None and planned_tools != current_tools:
            raise RuntimeError("microbial toolchain changed after execution planning")
        # Keep read-level candidates in the enclosing worker scratch tree.
        # The runner removes this tree even when the worker is group-killed.
        tmp_root = out.parent
        try:
            _check_contained(LabStore(ctx.root), tmp_root)
        except AssetError as exc:
            raise RuntimeError("microbial temporary directory is unsafe") from exc
        tmp_root.mkdir(parents=True, exist_ok=True)
        try:
            _check_contained(LabStore(ctx.root), tmp_root)
        except AssetError as exc:
            raise RuntimeError("microbial temporary directory is unsafe") from exc
        with tempfile.TemporaryDirectory(prefix="microbial-", dir=tmp_root) as temporary:
            work = Path(temporary)
            bam = ctx.input_dir / "aligned.bam"
            if ctx.derived:
                from .cell_origin import write_transient_bam

                bam = work / "derived.bam"
                write_transient_bam(ctx.input_dir / "aligned.bam", bam, dict(ctx.derived))
            reads = work / "candidate.fa"
            try:
                denominator, candidates, too_short, missing_sequence = _write_candidates(bam, reads, database.read_length)
                streamed = False
            except CandidateStagingExceeded:
                streamed = True
                candidates = 1  # The staging limit was crossed by an eligible read.
            taxa: list[dict[str, Any]] = []
            microbial_estimate = 0.0
            omitted = classified = unclassified = 0
            if candidates:
                report, abundance = work / "kraken.report", work / "bracken.tsv"
                if streamed:
                    denominator, candidates, too_short, missing_sequence = _classify_streamed(
                        bam, database.read_length, kraken[0], database.directory, report)
                else:
                    kraken_run = subprocess.run(
                        [kraken[0], "--db", str(database.directory), "--threads", "1", "--report", str(report),
                         "--output", "-", str(reads)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600, check=False,
                    )
                    if kraken_run.returncode != 0:
                        raise RuntimeError("Kraken2 classification failed")
                lineages, classified, unclassified, species_present = _report_lineages(report, candidates)
                if classified and species_present:
                    bracken_run = subprocess.run(
                        [bracken[0], "-d", str(database.directory), "-i", str(report), "-o", str(abundance),
                         "-r", str(database.read_length), "-l", "S", "-t", "1"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800, check=False,
                    )
                    if bracken_run.returncode != 0:
                        raise RuntimeError("Bracken abundance estimation failed")
                    taxa, microbial_estimate, omitted = _bracken_species(abundance, lineages, denominator)
            if inspect_database(LabStore(ctx.root)).sha256 != database.sha256:
                raise RuntimeError("microbial database changed during classification")
            if _tool("kraken2") != kraken or _tool("bracken") != bracken:
                raise RuntimeError("microbial toolchain changed during classification")
        assay_unavailable = candidates == 0 and (too_short + missing_sequence) > 0
        rpm = microbial_estimate * 1_000_000 / denominator if denominator and not assay_unavailable else None
        measurements = [
            metric("primary_reads", denominator, unit="reads"),
            metric("candidate_unmapped_reads", candidates, unit="reads", n=denominator),
            metric("too_short_unmapped_reads", too_short, unit="reads", n=denominator),
            metric("missing_sequence_unmapped_reads", missing_sequence, unit="reads", n=denominator),
            metric("classified_candidate_reads", classified, unit="reads", n=candidates),
            metric("unclassified_candidate_reads", unclassified, unit="reads", n=candidates),
            metric("microbial_estimated_reads", None if assay_unavailable or denominator == 0 else microbial_estimate,
                   unit="estimated reads", n=candidates),
            metric("microbial_rpm_primary", rpm, unit="estimated reads/million", n=denominator),
            metric("omitted_taxa", omitted, unit="taxa"),
        ]
        measurements.extend(metric(f"taxon_{row['taxid']}_rpm", row["rpm_primary"],
                                   unit="estimated reads/million", n=denominator) for row in taxa)
        profile = {"schema": "traceback.lab.microbial-profile.v1", "source_id": ctx.source_id,
                   "input_id": ctx.input_id, "database_sha256": database.sha256,
                   "database_source_label": database.source_label,
                   "read_length": database.read_length, "denominator": denominator,
                   "candidate_unmapped_reads": candidates, "too_short_unmapped_reads": too_short,
                   "missing_sequence_unmapped_reads": missing_sequence, "omitted_taxa": omitted,
                   "taxa": taxa, "interpretation": "exploratory; not an infection diagnosis"}
        return AlgorithmOutput(metrics=measurements,
                               artifacts={"microbial_profile.json": canonical_json_bytes(profile)})


MICROBIAL = Microbial("microbial")
