"""Exploratory EBV/HPV/HBV side alignment of human-unmapped primaries (D10).

The registered hg38 reference is untouched. Query names are replaced with
local serial numbers before a separate minimap2 process sees the reads; only
aggregate counts and unmapped-primary denominators enter the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam

from ..file_access import open_regular_no_follow
from ..pipeline import Stage, run_pipeline
from ..registry import AlgorithmOutput, InputContext, Ready, Skip, ToolPin
from ..viral_assets import VIRUSES, ViralReference, inspect_viral_reference
from ._base import Base, metric

UNIT = "reads / 1M unmapped primaries"
MIN_MINIMAP2 = (2, 28)


def _minimap2() -> tuple[str | None, str | None, tuple[int, int] | None]:
    """Discover the aligner without importing the signed CLI into a worker."""

    path = shutil.which("minimap2")
    if path is None:
        return None, None, None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return path, None, None
    match = re.match(r"(\d+)\.(\d+)([^\s]*)", result.stdout + result.stderr) if result.returncode == 0 else None
    if match is None:
        return path, None, None
    return path, match.group(0), (int(match.group(1)), int(match.group(2)))


def _derived_keep(ctx: InputContext, name: str) -> bool:
    if ctx.derived is None:
        return True
    from fractions import Fraction
    from math import ceil

    from ..read_summary import DS_SALT, SPLIT_SALT, h64

    transform = ctx.derived.get("transform")
    if transform == "downsample":
        threshold = ceil(Fraction(float(ctx.derived["fraction"])) * 2**64)
        return h64(DS_SALT, name) < threshold
    if transform == "split":
        return (h64(SPLIT_SALT, name) < 2**63) == (ctx.derived["half"] == "A")
    raise ValueError("viral-reads does not support this derived transform")


def _write_queries(ctx: InputContext, path: Path) -> tuple[int, int]:
    """Write anonymized FASTA queries; return primary denominator and queries."""

    denominator = 0
    queries = 0
    with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "rb") as bam, path.open("x", encoding="ascii") as output:
        for record in bam.fetch(until_eof=True):
            if record.is_secondary or record.is_supplementary or not record.is_unmapped:
                continue
            if not _derived_keep(ctx, record.query_name or ""):
                continue
            denominator += 1
            sequence = record.query_sequence
            if not sequence:
                continue
            queries += 1
            output.write(f">q{queries}\n{sequence}\n")
    return denominator, queries


def _copy_reference(reference: ViralReference, destination: Path) -> None:
    digest = hashlib.sha256()
    with os.fdopen(open_regular_no_follow(reference.fasta), "rb") as source, destination.open("xb") as target:
        while block := source.read(1 << 20):
            target.write(block)
            digest.update(block)
    if digest.hexdigest() != reference.sha256:
        raise RuntimeError("viral reference changed while staging for side alignment")


def _count_hits(paf: Path, reference: ViralReference, queries: int, db: Path) -> dict[str, int]:
    by_contig = {contig: virus for contig, virus, _ in reference.targets}
    counts = dict.fromkeys(VIRUSES, 0)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE hits (qid INTEGER NOT NULL, virus TEXT NOT NULL, PRIMARY KEY (qid, virus))")
        with paf.open("r", encoding="ascii") as stream:
            for line in stream:
                columns = line.rstrip("\n").split("\t")
                if len(columns) < 12 or not columns[0].startswith("q") or not columns[0][1:].isdigit():
                    raise RuntimeError("viral aligner returned malformed PAF")
                qid = int(columns[0][1:])
                virus = by_contig.get(columns[5])
                if not 1 <= qid <= queries or virus is None:
                    raise RuntimeError("viral aligner returned an unexpected query or contig")
                if "tp:A:P" not in columns[12:]:
                    continue
                connection.execute("INSERT OR IGNORE INTO hits VALUES (?, ?)", (qid, virus))
        # A query assigned to two virus groups is excluded from both. Multiple
        # primary segments within one group count only once.
        for virus, count in connection.execute(
            "SELECT virus, COUNT(*) FROM hits WHERE qid IN "
            "(SELECT qid FROM hits GROUP BY qid HAVING COUNT(*) = 1) GROUP BY virus"
        ):
            counts[virus] = int(count)
    return counts


@dataclass
class ViralReads(Base):
    family: str = "non-human"
    consumes: str = "full_bam"
    cost: str = "slow"
    upstream: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "side_alignment": "minimap2 -x map-ont --secondary=no -t 1",
        "input": "human_unmapped_primary_sequences",
        "denominator": "human_unmapped_primary_reads",
        "assignment": "one_virus_group_per_query",
    })

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        version = _minimap2()[1] or "missing"
        return (ToolPin("minimap2", version),)

    @tools.setter
    def tools(self, value: Any) -> None:
        pass

    def asset_sha256s(self, root: Path) -> tuple[str, ...]:
        from ..assets import AssetError
        from ..store import LabStore

        try:
            return inspect_viral_reference(LabStore(root)).asset_sha256s
        except AssetError:
            return ()

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        from ..assets import AssetError
        from ..store import LabStore

        if ctx.example:
            return Skip("example_reference", "viral-reads needs a registered EBV/HPV/HBV FASTA; not in the example")
        if ctx.reference_id != "hg38":
            return Skip("reference", "viral-reads keeps the registered hg38 human reference fixed")
        try:
            inspect_viral_reference(LabStore(ctx.root))
        except AssetError:
            return Skip("missing_asset", "viral FASTA unavailable: traceback lab asset add viral-fasta --file FASTA --targets TSV")
        path, _, numeric = _minimap2()
        if path is None or numeric is None or numeric < MIN_MINIMAP2:
            return Skip("missing_toolchain", "minimap2 >= 2.28 is needed for viral side alignment")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        reference = inspect_viral_reference(LabStore(ctx.root))
        path, _, numeric = _minimap2()
        if path is None or numeric is None or numeric < MIN_MINIMAP2:
            raise RuntimeError("minimap2 changed after viral-reads preflight")
        with tempfile.TemporaryDirectory(prefix="viral-", dir=out.parent) as temporary:
            work = Path(temporary)
            reads = work / "unmapped.fasta"
            denominator, queries = _write_queries(ctx, reads)
            counts = dict.fromkeys(VIRUSES, 0)
            if queries:
                staged_fasta = work / "viral.fasta"
                _copy_reference(reference, staged_fasta)
                paf = work / "hits.paf"
                result = run_pipeline(
                    [Stage("minimap2", (
                        path, "-x", "map-ont", "--secondary=no", "-t", "1",
                        str(staged_fasta), str(reads),
                    ))],
                    timeout_s=3600, env=os.environ, cwd=work, stdout_path=paf, disk_path=work,
                )
                if not result.ok:
                    raise RuntimeError("viral side alignment failed or timed out")
                counts = _count_hits(paf, reference, queries, work / "hits.sqlite")
            if inspect_viral_reference(LabStore(ctx.root)).asset_sha256s != reference.asset_sha256s:
                raise RuntimeError("viral reference changed during side alignment")
        metrics = [
            metric(f"{virus.lower()}_rpm", counts[virus] * 1_000_000 / denominator if denominator else None,
                   unit=UNIT, n=denominator)
            for virus in VIRUSES
        ]
        summary = {
            "denominator_unmapped_primaries": denominator,
            "queries_with_sequence": queries,
            "hits": counts,
            "reference_sha256": reference.sha256,
            "targets_sha256": reference.targets_sha256,
        }
        return AlgorithmOutput(metrics=metrics, artifacts={
            "viral_reads.json": json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("ascii")
        })


VIRAL_READS = ViralReads("viral-reads")
