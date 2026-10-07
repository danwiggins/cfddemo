"""The local site's view of one cell-origin record (signal CO5).

A pure function of the verified :class:`~traceback_runner.cell_origin.CellOriginMeasurementV1`
builds the analysis body that ``web/records.py`` wraps in the common envelope
(banner, identity, states).  Spec: ``docs/SIGNAL-METHODS-CELL-ORIGIN-AND-CNA.md``
§5 as amended by §11 (items 14, 15 and 17):

- evidence comes first: the basis line (fragments, markers, model) precedes the
  mixture;
- the ranked bar shows the top :data:`TOP_ROWS` contributors, then one
  "other contributors combined" row, always last and never a cell type;
- whiskers are off: an interval appears only in the full table, and only where
  its state is ``available``;
- a non-zero value below 0.1% reads "<0.1%";
- the residual line states its basis and that it has no threshold.

Every string is descriptive.  Nothing here qualifies the method or makes the
record fit for clinical use.  The catalog row never carries a fraction; only
this record view does (gate G1 is a process gate, see the spec).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from pydantic import Field

from .contracts import RunnerContract

#: The view schema the site's renderer is registered under.
VIEW_SCHEMA_VERSION = "traceback.local-cell-origin-view.v1"
#: Bars shown before the combined row (12 = ``DEFAULT_TOP_COMPOSITION_ROWS``,
#: ``evidence_inspector/cell_origin_pipeline.py``).
TOP_ROWS = 12
#: The catalog's key count unit for a cell-origin record.
KEY_COUNT_UNIT = "classified marker fragments"

HEADING = "Estimated cell-type mixture (Loyfer atlas)"
AXIS_LABEL = "Estimated fraction among registered atlas contributors"
SUM_NOTE = (
    "Fractions are forced to add up to 100%. This method has no 'unassigned' share."
)
RESIDUAL_NO_THRESHOLD = "It has no threshold; no value here is a judgement."

Fraction = Field(ge=0.0, le=1.0)


class IntervalView(RunnerContract):
    low: float = Fraction
    high: float = Fraction
    text: str


class ContributorRow(RunnerContract):
    rank: int = Field(ge=1)
    contributor_id: str
    fraction: float = Fraction
    fraction_text: str
    interval: IntervalView | None
    interval_state: Literal["available", "insufficient_information"]
    interval_label: str


class OtherRow(RunnerContract):
    """The combined row: always last, hatched, never a cell type."""

    contributors: int = Field(ge=0)
    at_zero: int = Field(ge=0)
    fraction: float = Fraction
    fraction_text: str
    label: str


class StateLine(RunnerContract):
    axis: Literal["cell_origin", "modbase"]
    token: str
    label: str
    meaning: str


class ModelLine(RunnerContract):
    id: str
    source: Literal["header", "operator_declared"]
    label: str


class CellOriginViewBody(RunnerContract):
    heading: Literal["Estimated cell-type mixture (Loyfer atlas)"] = HEADING
    axis_label: str = AXIS_LABEL
    basis: str
    model: ModelLine
    states: tuple[StateLine, ...]
    classified_fragments: int = Field(ge=0)
    mixed_fragments: int = Field(ge=0)
    eligible_alignments: int = Field(ge=0)
    observed_markers: int = Field(ge=0)
    registered_markers: int = Field(ge=0)
    denominator_lines: tuple[str, ...]
    top: tuple[ContributorRow, ...] = Field(max_length=TOP_ROWS)
    other: OtherRow
    contributors: tuple[ContributorRow, ...] = Field(min_length=1)
    whiskers: Literal[False] = False
    sum_note: str = SUM_NOTE
    residual_l2: float = Field(ge=0.0)
    residual_text: str


def _count(value: int) -> str:
    return f"{value:,}"


def percent_text(fraction: float) -> str:
    """One decimal, half to even; a non-zero value below 0.1% reads "<0.1%"."""

    if fraction == 0:
        return "0%"
    if fraction < 0.001:  # below 0.1%, whatever the rounding
        return "<0.1%"
    tenths = (Decimal(repr(fraction)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    if tenths == 0:
        return "<0.1%"
    return f"{tenths / 10:.1f}%"


def _model_label(source: str) -> str:
    return (
        "declared by the operator, not read from the file"
        if source == "operator_declared"
        else "read from the BAM header"
    )


def _public(text: str, fallback: str) -> str:
    """``text`` if it passes the site's public-text boundary, else ``fallback``.

    A registered ID can be valid for the record yet read as a path or an
    identifier to the boundary (``model..v1``); the view then names where the
    exact value is instead of failing the whole record with a 503.
    """

    from .web.contracts import validate_public_text

    try:
        validate_public_text(text)
    except ValueError:
        return fallback
    return text


def _copy(axis: str, token: str) -> tuple[str, str]:
    from .web.state_copy import copy_for

    return copy_for(axis, token)


def _row(rank: int, item: Any) -> ContributorRow:
    available = item.interval_state == "available" and item.interval is not None
    interval = (
        IntervalView(
            low=item.interval.low,
            high=item.interval.high,
            text=f"{percent_text(item.interval.low)} to {percent_text(item.interval.high)}",
        )
        if available
        else None
    )
    return ContributorRow(
        rank=rank,
        contributor_id=_public(
            item.contributor_id, f"Contributor {rank} (ID in the signed measurement file)"
        ),
        fraction=item.fraction,
        fraction_text=percent_text(item.fraction),
        interval=interval,
        interval_state=item.interval_state,
        interval_label=_copy("interval", "available" if available else "insufficient_information")[0],
    )


def build_body(measurement: Any) -> CellOriginViewBody:
    """The analysis body of one verified cell-origin measurement (pure)."""

    denominators = measurement.denominators
    ranked = sorted(measurement.estimates, key=lambda item: (-item.fraction, item.contributor_id))
    rows = tuple(_row(rank, item) for rank, item in enumerate(ranked, start=1))
    top, rest = rows[:TOP_ROWS], rows[TOP_ROWS:]
    rest_fraction = min(1.0, max(0.0, sum(item.fraction for item in rest)))
    at_zero = sum(item.fraction == 0 for item in rest)
    other = OtherRow(
        contributors=len(rest),
        at_zero=at_zero,
        fraction=rest_fraction,
        fraction_text=percent_text(rest_fraction) if rest else "none",
        label=(
            f"{len(rest)} other contributor{'s' if len(rest) != 1 else ''} combined "
            f"({at_zero} at 0%)"
            if rest
            else "No other contributors"
        ),
    )
    model = measurement.modbase_model
    model_text = (
        "model declared by operator"
        if model.source == "operator_declared"
        else "model read from the BAM header"
    )
    residual = measurement.solver.residual_l2
    return CellOriginViewBody(
        basis=(
            f"Based on {_count(denominators.classified_fragments)} fragments at "
            f"{_count(denominators.observed_markers)} of "
            f"{_count(denominators.registered_markers)} markers; {model_text}"
        ),
        model=ModelLine(
            id=_public(model.id, "(ID in the signed measurement file)"),
            source=model.source, label=_model_label(model.source)),
        states=tuple(
            StateLine(axis=axis, token=token, label=label, meaning=meaning)  # type: ignore[arg-type]
            for axis, token in (("cell_origin", "ready"), ("modbase", model.source))
            for label, meaning in (_copy(axis, token),)
        ),
        classified_fragments=denominators.classified_fragments,
        mixed_fragments=denominators.mixed_fragments,
        eligible_alignments=denominators.eligible_alignments,
        observed_markers=denominators.observed_markers,
        registered_markers=denominators.registered_markers,
        denominator_lines=(
            f"Based on {_count(denominators.classified_fragments)} classified fragments "
            f"at {_count(denominators.observed_markers)} of "
            f"{_count(denominators.registered_markers)} atlas markers",
            f"{_count(denominators.mixed_fragments)} mixed fragments not used",
            f"{_count(denominators.eligible_alignments)} eligible alignments",
        ),
        top=top,
        other=other,
        contributors=rows,
        residual_l2=residual,
        residual_text=(
            "How well the atlas explains the data is shown as the fit residual: "
            f"{residual:.4g} (the L2 norm of the weighted marker residuals of the "
            f"fit, row scale {measurement.solver.row_scale.replace('_', ' ')}). "
            f"{RESIDUAL_NO_THRESHOLD}"
        ),
    )


def key_count(measurement: Any) -> int:
    return int(measurement.denominators.classified_fragments)


__all__ = [
    "HEADING",
    "KEY_COUNT_UNIT",
    "TOP_ROWS",
    "VIEW_SCHEMA_VERSION",
    "CellOriginViewBody",
    "build_body",
    "key_count",
    "percent_text",
]
