"""MM/ML decoding shared by ``modbase-qc`` and the ``meth-calls`` intermediate.

pysam's ``modified_bases`` reports call positions on the **stored** sequence
(reference-forward orientation).  For a reverse-strand record (and for an
opposite-strand ``G-m`` call) the call sits on the G of the stored sequence,
so a call is a CpG call when it sits on either base of a stored ``CG``.  An
MM call without an ML value (pysam reports -1) makes the record invalid: an
unknown probability is never read as canonical.

Validity (D28): a record whose ``MN`` differs from its stored sequence length
(hard-clipped supplementary records after ``minimap2 -y``), or whose MM/ML do
not parse, contributes no calls and is counted as ``invalid_mm``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# One MM entry: base, strand, one or more codes (letters or a ChEBI number),
# optional skip mode, then the delta list.
_MM_ENTRY = re.compile(r"([ACGTUN])([-+])([a-z]+|\d+)([.?]?)")


def mm_codes(mm: str) -> list[str]:
    """Mod codes an MM string declares, e.g. ``"C+h?,1;C+m?,1;"`` → ``["C+h", "C+m"]``.

    A multi-code entry such as ``C+hm`` declares both ``C+h`` and ``C+m``.
    """

    codes: list[str] = []
    for entry in mm.split(";"):
        head = entry.split(",", 1)[0].strip()
        match = _MM_ENTRY.fullmatch(head)
        if not match:
            continue
        base, strand, letters, _ = match.groups()
        parts = [letters] if letters.isdigit() else list(letters)
        for code in parts:
            name = f"{base}{strand}{code}"
            if name not in codes:
                codes.append(name)
    return codes


def mm_skip_modes(mm: str) -> dict[str, str]:
    """Skip mode per code: ``"."`` (implicit canonical; also the default) or ``"?"`` (missing)."""

    modes: dict[str, str] = {}
    for entry in mm.split(";"):
        head = entry.split(",", 1)[0].strip()
        match = _MM_ENTRY.fullmatch(head)
        if not match:
            continue
        base, strand, letters, mode = match.groups()
        parts = [letters] if letters.isdigit() else list(letters)
        for code in parts:
            modes[f"{base}{strand}{code}"] = mode or "."
    return modes


def mm_tag(record: Any) -> str | None:
    for tag in ("MM", "Mm"):
        if record.has_tag(tag):
            return str(record.get_tag(tag))
    return None


def mn_ok(record: Any) -> bool:
    if not record.has_tag("MN"):
        return True
    return int(record.get_tag("MN")) == len(record.query_sequence or "")


@dataclass(frozen=True)
class SiteCall:
    """One CpG call on a record: stored-sequence position and per-code ML (0-255)."""

    qpos: int  # position on the stored sequence (the C forward, the G reverse)
    probabilities: dict[str, int]  # code letter ("m", "h") -> ML value

    @property
    def p_modified(self) -> int:
        return sum(self.probabilities.values())

    @property
    def p_canonical(self) -> int:
        return max(0, 255 - self.p_modified)

    def max_probability(self) -> float:
        return max([*self.probabilities.values(), self.p_canonical]) / 255.0


class InvalidMM(ValueError):
    pass


def cpg_calls(record: Any) -> Iterator[SiteCall]:
    """CpG calls of one record (C+m / C+h), merged per position.

    Raises :class:`InvalidMM` when ``MN`` mismatches or MM/ML do not parse.
    """

    if not mn_ok(record):
        raise InvalidMM("MN differs from the stored sequence length")
    sequence = record.query_sequence or ""
    try:
        parsed = record.modified_bases
    except (ValueError, KeyError, IndexError) as exc:  # pysam raises on malformed tags
        raise InvalidMM(str(exc)) from exc
    if parsed is None:
        if mm_tag(record) is not None:
            raise InvalidMM("MM/ML do not parse")
        return
    merged: dict[int, dict[str, int]] = {}
    for (base, _strand, code), calls in parsed.items():
        if base not in ("C", "G") or code not in ("m", "h"):
            continue
        for position, quality in calls:
            if quality < 0:  # pysam reports -1 when ML is absent: unknown, not canonical
                raise InvalidMM("MM call without an ML probability")
            # Orientation-agnostic CpG test on the stored sequence: the call sits
            # on the C or on the G of a CG dinucleotide (reverse-strand records
            # and opposite-strand G-m/G-h calls land on the G).
            on_c = position + 1 < len(sequence) and sequence[position] == "C" and sequence[position + 1] == "G"
            on_g = 0 < position < len(sequence) and sequence[position - 1] == "C" and sequence[position] == "G"
            if not (on_c or on_g):
                continue
            merged.setdefault(position, {})[code] = int(quality)
    for position in sorted(merged):
        yield SiteCall(position, merged[position])


__all__ = ["InvalidMM", "SiteCall", "cpg_calls", "mm_codes", "mm_skip_modes", "mm_tag", "mn_ok"]
