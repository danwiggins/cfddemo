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
        platform_tags = [part[3:] for part in line.split("\t")[1:] if part.startswith("PL:")]
        if len(platform_tags) != 1:
            declared.add("unknown")
            continue
        value = platform_tags[0].upper()
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

    if not isinstance(manifest, Mapping):
        raise ValueError("stored Lab input manifest is not an object")
    kind = manifest.get("kind")
    if kind == "aligned":
        if manifest.get("platform", "ont") != "ont":
            raise ValueError("Lab-aligned input platform must be ONT")
        actual = from_bam(bam_path)
        if actual != "ont":
            # Earlier Lab versions carried source read groups through map-ont
            # without requiring a recognized PL. Match the exact recorded
            # recipe for the BAM's read-group count, but reject any explicit
            # Illumina declaration.
            from .ingest import aligner_argv_identity

            if "platform" in manifest or actual != "unknown":
                raise ValueError("Lab-aligned BAM does not declare ONT")
            with pysam.AlignmentFile(str(bam_path), "rb", check_sq=False) as bam:
                rg_lines = [line for line in str(bam.header).splitlines() if line.startswith("@RG\t")]
            has_illumina = any(
                tag.upper() == "PL:ILLUMINA"
                for line in rg_lines for tag in line.split("\t")[1:]
            )
            if has_illumina or manifest.get("aligner_argv") != aligner_argv_identity(rg_lines):
                raise ValueError("Lab-aligned BAM does not declare ONT")
        return "ont"
    if kind != "prealigned":
        raise ValueError("unknown Lab input kind")
    actual = from_bam(bam_path)
    # Old prealigned manifests have no platform field. Their unanimous BAM
    # declaration remains readable; new manifests pin it explicitly.
    declared = manifest.get("platform", actual)
    if declared not in PLATFORMS or declared != actual:
        raise ValueError("prealigned platform declaration differs from BAM read groups")
    return actual


__all__ = ["PLATFORMS", "Platform", "for_input", "from_bam", "from_rg_lines"]
