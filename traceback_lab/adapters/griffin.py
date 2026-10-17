"""Read Griffin's own GC-corrected nucleosome-profile TSV.

This is an interchange adapter, not a reimplementation of Griffin. In
particular, uncorrected coverage and arbitrary site-density tracks must not
be presented as a Griffin measurement. The upstream workflow creates the TSV
from an aligned BAM, an hg38 reference, a sample GC-bias file, and filtered
site lists; those inputs need separate per-sample provenance at publication.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path


class GriffinFormatError(ValueError):
    """The file does not satisfy the upstream corrected-profile contract."""


@dataclass(frozen=True)
class GriffinProfile:
    sample: str
    site_name: str
    site_count: int
    offsets_bp: tuple[int, ...]
    coverage: tuple[float, ...]
    central_coverage: float
    amplitude: float
    smoothing: bool
    cna_normalization: bool


_FIELDS = {
    "sample", "site_name", "number_of_sites", "correction",
    "central_coverage", "amplitude", "smoothing", "CNA_normalization",
}


def _boolean(value: str, field: str) -> bool:
    if value not in ("True", "False"):
        raise GriffinFormatError(f"{field} must be True or False")
    return value == "True"


def _finite(value: str, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GriffinFormatError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise GriffinFormatError(f"{field} must be finite")
    return number


def read_corrected_profiles(path: Path, *, expected_sample: str | None = None) -> tuple[GriffinProfile, ...]:
    """Parse Griffin v0.2-style `*.GC_corrected.coverage.tsv` strictly.

    Coordinate headers are *bin starts* in bp, not bin centers. The upstream
    pipeline can configure its window and step, so this validates a regular
    coordinate grid without assuming a particular window width.
    """

    if not path.name.endswith(".GC_corrected.coverage.tsv"):
        raise GriffinFormatError("expected Griffin's GC_corrected.coverage.tsv output")
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        fields = reader.fieldnames
        if fields is None or len(fields) != len(set(fields)) or not _FIELDS.issubset(fields):
            raise GriffinFormatError("missing or duplicate Griffin output columns")
        try:
            offsets = tuple(int(field) for field in fields if field.lstrip("-").isdigit())
        except ValueError as exc:
            raise GriffinFormatError("invalid coverage coordinate") from exc
        if len(offsets) < 3 or sorted(offsets) != list(offsets):
            raise GriffinFormatError("coverage coordinates must increase")
        step = offsets[1] - offsets[0]
        if step <= 0 or any(b - a != step for a, b in zip(offsets, offsets[1:])):
            raise GriffinFormatError("coverage coordinates must form a regular grid")
        profiles: list[GriffinProfile] = []
        names: set[str] = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise GriffinFormatError("ragged Griffin output row")
            if row["correction"] != "GC_corrected":
                raise GriffinFormatError("uncorrected or differently corrected Griffin output")
            sample = row["sample"].strip()
            site_name = row["site_name"].strip()
            if not sample or not site_name or (expected_sample is not None and sample != expected_sample):
                raise GriffinFormatError("sample or site-list name does not match")
            if site_name in names:
                raise GriffinFormatError("duplicate site-list name")
            names.add(site_name)
            try:
                count = int(row["number_of_sites"])
            except ValueError as exc:
                raise GriffinFormatError("number_of_sites must be an integer") from exc
            if count <= 0:
                raise GriffinFormatError("number_of_sites must be positive")
            coverage = tuple(_finite(row[str(offset)], str(offset)) for offset in offsets)
            if any(value < 0 for value in coverage):
                raise GriffinFormatError("coverage must be nonnegative")
            central = _finite(row["central_coverage"], "central_coverage")
            amplitude = _finite(row["amplitude"], "amplitude")
            if central < 0 or amplitude < 0:
                raise GriffinFormatError("Griffin features must be nonnegative")
            profiles.append(GriffinProfile(
                sample=sample, site_name=site_name, site_count=count,
                offsets_bp=offsets, coverage=coverage, central_coverage=central,
                amplitude=amplitude,
                smoothing=_boolean(row["smoothing"], "smoothing"),
                cna_normalization=_boolean(row["CNA_normalization"], "CNA_normalization"),
            ))
    if not profiles:
        raise GriffinFormatError("no Griffin profiles")
    return tuple(profiles)
