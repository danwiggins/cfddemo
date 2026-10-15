"""Record-level helpers for algorithms that stream the aligned BAM.

Most Lab algorithms read the D33 read summary only.  The few that need a
per-record field the summary does not carry (MM/ML calls, reference bases)
stream ``aligned.bam`` and must apply the same two predicates as the
summary: D21 eligibility, and the derived input's row mask, re-derived from
the read-name hash (D33), so a downsample or split half sees exactly the
rows its mask keeps.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Mapping
from fractions import Fraction
from math import ceil
from typing import Any

from ..identity import validate_fraction
from ..read_summary import DS_SALT, FLAG_EXCLUDE, MIN_MAPQ, SPLIT_SALT, h64


def name_predicate(derived: Mapping[str, Any] | None) -> Callable[[str], bool] | None:
    """The derived input's row predicate on a read name (``None`` keeps every row)."""

    if not derived:
        return None
    transform = derived.get("transform")
    if transform == "downsample":
        threshold = ceil(Fraction(validate_fraction(float(derived["fraction"]))) * 2**64)
        return lambda name: h64(DS_SALT, name) < threshold
    if transform == "split":
        if derived.get("half") not in {"A", "B"}:
            raise ValueError("a split half is A or B")
        want_a = derived["half"] == "A"
        return lambda name: (h64(SPLIT_SALT, name) < 2**63) == want_a
    raise ValueError(f"no row predicate for transform {transform!r}")


def eligible_record(record: Any, contigs: Container[str]) -> bool:
    """D21: primary, mapped, not QC-fail or duplicate, MAPQ >= 20, on ``contigs``."""

    return (
        not record.flag & FLAG_EXCLUDE
        and record.mapping_quality >= MIN_MAPQ
        and record.reference_name in contigs
    )


__all__ = ["eligible_record", "name_predicate"]
