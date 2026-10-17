"""Explicit Lab input platform from BAM read-group declarations.

This is provenance declared by a BAM header, not instrument attestation.
The Lab's own map-ont alignment is ONT; prealigned inputs need unanimous PL
declarations before any platform-specific algorithm can run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

import pysam

Platform = Literal["ont", "illumina", "unknown"]
PLATFORMS = frozenset({"ont", "illumina", "unknown"})


def from_rg_lines(lines: Iterable[str]) -> Platform:
    declared: set[str] = set()
    seen = False
    for line in lines:
        if not line.startswith("@RG\t"):
            continue
        seen = True
        fields = dict(part.split(":", 1) for part in line.split("\t")[1:] if ":" in part)
        value = fields.get("PL", "").upper()
        if value in {"ONT", "OXFORD_NANOPORE"}:
            declared.add("ont")
        elif value == "ILLUMINA":
            declared.add("illumina")
        else:
            declared.add("unknown")
    return next(iter(declared)) if seen and len(declared) == 1 and "unknown" not in declared else "unknown"


def from_bam(path: Path) -> Platform:
    with pysam.AlignmentFile(str(path), "rb", check_sq=False) as bam:
        return from_rg_lines(str(bam.header).splitlines())


def for_input(manifest: Mapping[str, Any], bam_path: Path) -> Platform:
    """Resolve old manifests safely and verify new ones against their BAM."""

    kind = manifest.get("kind")
    if kind == "aligned":
        if manifest.get("platform", "ont") != "ont":
            raise ValueError("Lab-aligned input platform must be ONT")
        actual = from_bam(bam_path)
        if actual != "ont":
            # Earlier Lab versions could align an unaligned BAM without @RG.
            # Accept only that exact old map-ont recipe and an absent RG;
            # ambiguous or contrary declarations still fail closed.
            from .ingest import ALIGNER_IDENTITY_ARGV

            legacy = "platform" not in manifest and manifest.get("aligner_argv") == list(ALIGNER_IDENTITY_ARGV)
            if not legacy or actual != "unknown":
                raise ValueError("Lab-aligned BAM does not declare ONT")
            with pysam.AlignmentFile(str(bam_path), "rb", check_sq=False) as bam:
                if bam.header.get("RG"):
                    raise ValueError("Lab-aligned BAM does not declare ONT")
        return "ont"
    if kind != "prealigned":
        raise ValueError("unknown Lab input kind")
    actual = from_bam(bam_path)
    # An old prealigned manifest had no platform field. It used the ONT
    # identity, so its BAM may confirm ONT but may not redefine that identity.
    declared = manifest.get("platform", "ont")
    if declared not in PLATFORMS or declared != actual:
        raise ValueError("prealigned platform declaration differs from BAM read groups")
    return actual


__all__ = ["PLATFORMS", "Platform", "for_input", "from_bam", "from_rg_lines"]
