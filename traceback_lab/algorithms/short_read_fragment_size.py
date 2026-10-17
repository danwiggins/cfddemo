"""Exploratory paired short-read fragment length from absolute BAM TLEN.

This is separate from ONT ``fragment-size``: its aligned-span definition and
interpretation remain untouched. Only prealigned BAMs whose read groups all
declare Illumina are eligible. A qualifying first mate represents one pair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam

from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, metric
from ._records import eligible_record, name_predicate
from .fragment_size import HIST_MAX, summarize

_CANONICAL = re.compile(r"^chr([1-9]|1[0-9]|2[0-2]|X|Y)$")


def _declared_illumina(bam: pysam.AlignmentFile) -> bool:
    groups = bam.header.to_dict().get("RG", [])
    return bool(groups) and all(group.get("PL", "").upper() == "ILLUMINA" for group in groups)


def _contigs(bam: pysam.AlignmentFile) -> set[str]:
    canonical = {name for name in bam.references if _CANONICAL.fullmatch(name)}
    return canonical or set(bam.references)  # toy-reference parity with D21


def _tlen_counts(ctx: InputContext) -> dict[int, int]:
    counts: dict[int, int] = {}
    keep_name = name_predicate(ctx.derived)
    with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "rb") as bam:
        if not _declared_illumina(bam):
            raise RuntimeError("short-read BAM no longer declares one Illumina platform")
        contigs = _contigs(bam)
        for record in bam.fetch(until_eof=True):
            if not (
                record.is_paired and record.is_proper_pair and record.is_read1
                and not record.mate_is_unmapped
                and record.reference_id == record.next_reference_id
                and eligible_record(record, contigs)
                and record.template_length != 0
            ):
                continue
            if keep_name is not None and not keep_name(record.query_name or ""):
                continue
            # The public histogram stops at 1,000 bp; one overflow bin keeps
            # memory bounded without treating longer templates as short.
            length = min(abs(record.template_length), HIST_MAX + 1)
            counts[length] = counts.get(length, 0) + 1
    return counts


@dataclass
class ShortReadFragmentSize(Base):
    family: str = "fragmentomics"
    requires: Requirements = field(default_factory=lambda: Requirements(platforms=frozenset({"illumina"})))
    consumes: str = "full_bam"
    cost: str = "slow"
    upstream: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "length": "abs(TLEN)",
        "molecule": "one proper first mate, mate mapped on same contig",
        "filter": "D21 on first mate; MAPQ >= 20; primary, nonduplicate, QC-pass",
        "platform": "prealigned BAM; all @RG PL=ILLUMINA",
        "histogram_max_bp": HIST_MAX,
    })

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example or ctx.reference_id != "hg38":
            return Skip("reference", "short-read TLEN needs a registered hg38 input")
        try:
            manifest = json.loads((ctx.input_dir / "manifest.json").read_bytes())
        except (OSError, ValueError):
            return Skip("platform", "short-read TLEN needs a stored prealigned BAM")
        if manifest.get("kind") != "prealigned":
            return Skip("platform", "short-read TLEN needs a prealigned BAM; Lab alignment is ONT-only")
        try:
            with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "rb") as bam:
                if not _declared_illumina(bam):
                    return Skip("platform", "all BAM read groups must declare PL:ILLUMINA")
        except (OSError, ValueError):
            return Skip("platform", "short-read TLEN needs a readable prealigned BAM")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        del out
        stats = summarize(_tlen_counts(ctx))
        hist = stats["hist"]
        panel = {"tlen_bp": list(range(1, HIST_MAX + 1)), "count": [int(value) for value in hist[1:]]}
        return AlgorithmOutput(
            metrics=[
                metric("mode_bp", stats["mode_bp"], unit="bp", n=stats["n_mode"]),
                metric("short_fraction_lt150", stats["short_fraction_lt150"], unit="fraction", n=stats["n_base"]),
                metric("peak_mono_bp", stats["peak_mono_bp"], unit="bp", n=stats["n_mode"]),
                metric("peak_di_bp", stats["peak_di_bp"], unit="bp", n=stats["n_di"]),
                metric("periodicity_10bp_power", stats["periodicity_10bp_power"],
                       unit="fraction", n=stats["n_periodicity"]),
            ],
            panels=[{"panel_id": "short-read-size-density", "kind": "density", "data_ref": "artifacts/tlen_hist.json"}],
            artifacts={"tlen_hist.json": json.dumps(panel, separators=(",", ":")).encode("ascii")},
        )


SHORT_READ_FRAGMENT_SIZE = ShortReadFragmentSize("short-read-fragment-size")
