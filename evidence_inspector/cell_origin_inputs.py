"""Validated, bounded loaders for local cell-origin scientific inputs.

These loaders deliberately require explicit column mappings. They never expose
input paths or raw fragment identifiers in returned objects or error messages.
"""

from __future__ import annotations

import csv
import hashlib
import math
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Iterator, Mapping, Sequence

from pydantic import Field, ValidationError, model_validator

from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    AtlasUMatrixRow,
    AtlasUValue,
    CpgCallState,
    GenomicMarker,
    LOYFER_UXM_METHOD,
    MethodDefinition,
    ModProbabilityPolicy,
    ModkitCpgCall,
    ModkitCpgCallV2,
    ModkitIngestionLedgerV2,
    ModkitInputProvenanceV2,
    ModkitInputResultV2,
    ModkitSourceSchema,
    Strand,
    StrictModel,
)

DEFAULT_MAX_ROWS = 1_000_000

ReferenceContextProvider = Callable[[str, int, int], str]


class CellOriginInputError(ValueError):
    """A sanitized validation error for a scientific input."""


class CoordinateSystem(StrEnum):
    ZERO_BASED = "zero_based"
    BED_ZERO_BASED_HALF_OPEN = "bed_zero_based_half_open"


class FractionUnit(StrEnum):
    FRACTION = "fraction"
    PERCENT = "percent"


@dataclass(frozen=True)
class ModkitExtractColumns:
    """Column names for a normalized ``modkit extract calls`` TSV."""

    fragment_id: str
    chromosome: str
    position0: str
    strand: str
    modified_primary_base: str
    call_code: str
    modified_probability: str
    fail: str
    ignored: tuple[str, ...] = ()

    def declared(self) -> tuple[str, ...]:
        return (
            self.fragment_id,
            self.chromosome,
            self.position0,
            self.strand,
            self.modified_primary_base,
            self.call_code,
            self.modified_probability,
            self.fail,
            *self.ignored,
        )


@dataclass(frozen=True)
class GenericHardCallColumnsV2:
    """Exact columns for the declared generic hard-call CpG schema."""

    fragment_id: str
    chromosome: str
    position0: str
    modification_strand: str
    reference_mod_strand: str
    modified_primary_base: str
    call_code: str
    selected_state_probability: str
    fail: str
    ignored: tuple[str, ...] = ()

    def declared(self) -> tuple[str, ...]:
        return (
            self.fragment_id,
            self.chromosome,
            self.position0,
            self.modification_strand,
            self.reference_mod_strand,
            self.modified_primary_base,
            self.call_code,
            self.selected_state_probability,
            self.fail,
            *self.ignored,
        )


@dataclass(frozen=True)
class GenericCmhProbabilityColumnsV1:
    """Exact columns for generic canonical-C, 5mC, and 5hmC probabilities."""

    fragment_id: str
    chromosome: str
    position0: str
    modification_strand: str
    reference_mod_strand: str
    modified_primary_base: str
    canonical_probability: str
    methyl_probability: str
    hydroxymethyl_probability: str
    fail: str
    ignored: tuple[str, ...] = ()

    def declared(self) -> tuple[str, ...]:
        return (
            self.fragment_id,
            self.chromosome,
            self.position0,
            self.modification_strand,
            self.reference_mod_strand,
            self.modified_primary_base,
            self.canonical_probability,
            self.methyl_probability,
            self.hydroxymethyl_probability,
            self.fail,
            *self.ignored,
        )


@dataclass(frozen=True)
class AtlasUColumns:
    """Column names and exact cell IDs for a Loyfer U-matrix TSV."""

    marker_id: str
    cell_type_columns: tuple[tuple[str, str], ...]
    ignored: tuple[str, ...] = ()

    def declared(self) -> tuple[str, ...]:
        return (
            self.marker_id,
            *(column for _, column in self.cell_type_columns),
            *self.ignored,
        )


@dataclass(frozen=True)
class MarkerBedColumns:
    """Zero-based column indexes for a headerless marker BED."""

    chromosome: int
    start0: int
    end0: int
    marker_id: int
    target_cell_type_id: int
    ignored: tuple[int, ...] = ()

    def declared(self) -> tuple[int, ...]:
        return (
            self.chromosome,
            self.start0,
            self.end0,
            self.marker_id,
            self.target_cell_type_id,
            *self.ignored,
        )


@dataclass(frozen=True)
class HealthyTableS8Columns:
    """Column names and exact sample IDs for normalized Table S8 records."""

    cell_type_id: str
    sample_fraction_columns: tuple[tuple[str, str], ...]
    ignored: tuple[str, ...] = ()

    def declared(self) -> tuple[str, ...]:
        return (
            self.cell_type_id,
            *(column for _, column in self.sample_fraction_columns),
            *self.ignored,
        )


class HealthySampleFraction(StrictModel):
    sample_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class HealthyTableS8Row(StrictModel):
    cell_type_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    sample_fractions: tuple[HealthySampleFraction, ...] = Field(min_length=1)
    min_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    max_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_values(self) -> HealthyTableS8Row:
        sample_ids = [item.sample_id for item in self.sample_fractions]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("healthy sample IDs must be unique")
        values = [item.fraction for item in self.sample_fractions]
        if self.min_fraction != min(values) or self.max_fraction != max(values):
            raise ValueError("healthy range must equal the observed sample range")
        return self


class HealthyTableS8(StrictModel):
    """Normalized plasma rows from Loyfer Supplementary Table S8."""

    method: MethodDefinition
    source_ids: tuple[str, ...] = Field(min_length=1)
    sample_ids: tuple[str, ...] = Field(min_length=1)
    rows: tuple[HealthyTableS8Row, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_table(self) -> HealthyTableS8:
        if self.method != LOYFER_UXM_METHOD:
            raise ValueError("Table S8 rows require the Loyfer UXM method")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source IDs must be unique")
        if len(self.sample_ids) != len(set(self.sample_ids)):
            raise ValueError("healthy sample IDs must be unique")
        cell_ids = [row.cell_type_id for row in self.rows]
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("healthy cell type IDs must be unique")
        for row in self.rows:
            observed = tuple(item.sample_id for item in row.sample_fractions)
            if observed != self.sample_ids:
                raise ValueError(
                    "every healthy row must contain declared samples in order"
                )
        return self


TextSource = str | PathLike[str] | IO[str]


@contextmanager
def _open_text(source: TextSource) -> Iterator[IO[str]]:
    if hasattr(source, "read"):
        yield source  # type: ignore[misc]
        return
    try:
        with Path(source).open("r", encoding="utf-8", newline="") as handle:
            yield handle
    except (OSError, UnicodeError) as exc:
        raise CellOriginInputError("unable to read scientific input") from exc


def _validate_cap(max_rows: int) -> None:
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise CellOriginInputError("max_rows must be a positive integer")


def _validate_fraction_unit(unit: FractionUnit) -> None:
    if not isinstance(unit, FractionUnit):
        raise CellOriginInputError("unsupported fraction unit")


def _validate_declared_columns(
    actual: Sequence[str],
    declared: Sequence[str],
) -> None:
    if not actual:
        raise CellOriginInputError("input is missing a header")
    if any(not item for item in declared):
        raise CellOriginInputError("column mappings cannot be empty")
    if len(declared) != len(set(declared)):
        raise CellOriginInputError("column mappings must be unique")
    if len(actual) != len(set(actual)):
        raise CellOriginInputError("input header contains duplicate columns")
    if set(actual) != set(declared):
        missing = sorted(set(declared) - set(actual))
        unknown = sorted(set(actual) - set(declared))
        details = []
        if missing:
            details.append(f"missing columns: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown columns: {', '.join(unknown)}")
        raise CellOriginInputError("; ".join(details))


def _bounded_rows(
    rows: Iterable[Any],
    *,
    max_rows: int,
) -> Iterator[tuple[int, Any]]:
    _validate_cap(max_rows)
    for row_number, row in enumerate(rows, start=1):
        if row_number > max_rows:
            raise CellOriginInputError(f"input exceeds row cap of {max_rows}")
        yield row_number, row


def _required_text(value: Any, field: str, row_number: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CellOriginInputError(
            f"{field} must be nonempty text without surrounding whitespace "
            f"at row {row_number}"
        )
    return value


def _integer(value: Any, field: str, row_number: int) -> int:
    text = _required_text(value, field, row_number)
    try:
        parsed = int(text, 10)
    except ValueError as exc:
        raise CellOriginInputError(
            f"{field} must be an integer at row {row_number}"
        ) from exc
    if str(parsed) != text:
        raise CellOriginInputError(
            f"{field} must use canonical integer syntax at row {row_number}"
        )
    return parsed


def _fraction(
    value: Any,
    field: str,
    row_number: int,
    *,
    unit: FractionUnit = FractionUnit.FRACTION,
) -> float:
    if isinstance(value, bool):
        raise CellOriginInputError(f"{field} must be numeric at row {row_number}")
    if isinstance(value, str):
        if not value or value != value.strip():
            raise CellOriginInputError(
                f"{field} must be numeric at row {row_number}"
            )
        try:
            parsed = float(value)
        except ValueError as exc:
            raise CellOriginInputError(
                f"{field} must be numeric at row {row_number}"
            ) from exc
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        raise CellOriginInputError(f"{field} must be numeric at row {row_number}")
    if not math.isfinite(parsed):
        raise CellOriginInputError(f"{field} must be finite at row {row_number}")
    if unit == FractionUnit.PERCENT:
        parsed /= 100.0
    if not 0.0 <= parsed <= 1.0:
        raise CellOriginInputError(
            f"{field} must resolve to a fraction within [0, 1] "
            f"at row {row_number}"
        )
    return parsed


def _false_or_true(value: Any, row_number: int) -> bool:
    text = _required_text(value, "fail", row_number).lower()
    if text == "false":
        return False
    if text == "true":
        return True
    raise CellOriginInputError(
        f"fail must be exactly true or false at row {row_number}"
    )


def _validate_method(
    observed: MethodDefinition,
    expected: MethodDefinition,
    input_name: str,
) -> None:
    if observed != expected:
        raise CellOriginInputError(
            f"{input_name} has an incompatible scientific method identity"
        )


def _validate_exact_ids(
    observed: Sequence[str],
    expected: Sequence[str] | None,
    kind: str,
) -> None:
    if expected is None:
        return
    if len(expected) != len(set(expected)):
        raise CellOriginInputError(f"expected {kind} IDs must be unique")
    if set(observed) != set(expected):
        raise CellOriginInputError(f"{kind} IDs do not exactly match registry")


def _model_error(input_name: str, row_number: int, exc: Exception) -> None:
    raise CellOriginInputError(
        f"{input_name} failed schema validation at row {row_number}"
    ) from exc


def _strand(value: Any, field: str, row_number: int) -> Strand:
    text = _required_text(value, field, row_number)
    try:
        return Strand(text)
    except ValueError as exc:
        raise CellOriginInputError(
            f"{field} must be + or - at row {row_number}"
        ) from exc


def _fragment_digest(raw_id: Any, salt: bytes, row_number: int) -> str:
    raw = _required_text(raw_id, "fragment_id", row_number)
    return hashlib.sha256(salt + b"\0" + raw.encode("utf-8")).hexdigest()


def _validate_v2_common(
    *,
    fragment_hash_salt: bytes,
    reference_context_provider: ReferenceContextProvider,
) -> None:
    if not isinstance(fragment_hash_salt, bytes) or not fragment_hash_salt:
        raise CellOriginInputError("fragment_hash_salt must be nonempty bytes")
    if not callable(reference_context_provider):
        raise CellOriginInputError("reference context provider must be callable")


def _canonical_cpg_position(
    *,
    chromosome: str,
    original_position0: int,
    reference_mod_strand: Strand,
    reference_context_provider: ReferenceContextProvider,
    row_number: int,
) -> int:
    if reference_mod_strand == Strand.PLUS:
        canonical_position0 = original_position0
    else:
        if original_position0 == 0:
            raise CellOriginInputError(
                f"minus-strand CpG position underflows at row {row_number}"
            )
        canonical_position0 = original_position0 - 1
    try:
        context = reference_context_provider(
            chromosome,
            canonical_position0,
            canonical_position0 + 2,
        )
    except Exception as exc:
        raise CellOriginInputError(
            f"reference context provider failed at row {row_number}"
        ) from exc
    if not isinstance(context, str) or context.upper() != "CG":
        raise CellOriginInputError(
            f"reference context is not a CpG dyad at row {row_number}"
        )
    return canonical_position0


def _v2_call_key(call: ModkitCpgCallV2) -> tuple[str, str, int, Strand, Strand]:
    return (
        call.fragment_digest,
        call.chromosome,
        call.canonical_cpg_position0,
        call.modification_strand,
        call.reference_mod_strand,
    )


def _v2_result(
    *,
    provenance: ModkitInputProvenanceV2,
    calls: Sequence[ModkitCpgCallV2],
    counters: Mapping[str, int],
) -> ModkitInputResultV2:
    try:
        ledger = ModkitIngestionLedgerV2(
            policy=provenance.policy,
            malformed_rows=0,
            duplicate_rows=0,
            **counters,
        )
        return ModkitInputResultV2(
            schema_version="cell-origin-modkit-input.v2",
            provenance=provenance,
            ledger=ledger,
            calls=tuple(calls),
        )
    except ValidationError as exc:
        raise CellOriginInputError(
            "generic CpG ingestion failed aggregate validation"
        ) from exc


def load_generic_hard_call_cpg_v2(
    source: TextSource,
    *,
    columns: GenericHardCallColumnsV2,
    provenance: ModkitInputProvenanceV2,
    fragment_hash_salt: bytes,
    reference_context_provider: ReferenceContextProvider,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ModkitInputResultV2:
    """Load the declared generic hard-call schema without claiming Modkit parity."""

    if provenance.policy != ModProbabilityPolicy.HARD_CALL_COLLAPSED_M_H:
        raise CellOriginInputError("hard-call loader requires hard-call provenance")
    if provenance.source_schema_id != ModkitSourceSchema.GENERIC_HARD_CALL_CPG_V2:
        raise CellOriginInputError("hard-call loader requires its generic schema")
    _validate_v2_common(
        fragment_hash_salt=fragment_hash_salt,
        reference_context_provider=reference_context_provider,
    )
    counters = {
        "total_rows": 0,
        "source_failed_rows": 0,
        "source_passed_rows": 0,
        "excluded_non_c_rows": 0,
        "candidate_c_rows": 0,
        "hard_call_c_rows": 0,
        "hard_call_m_rows": 0,
        "hard_call_h_rows": 0,
        "probability_input_rows": 0,
        "excluded_probability_tie_rows": 0,
        "excluded_low_confidence_rows": 0,
        "eligible_call_rows": 0,
        "unmethylated_call_rows": 0,
        "methylated_call_rows": 0,
        "reference_plus_call_rows": 0,
        "reference_minus_call_rows": 0,
    }
    calls: list[ModkitCpgCallV2] = []
    seen: set[tuple[str, str, int, Strand, Strand]] = set()
    with _open_text(source) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_declared_columns(reader.fieldnames or (), columns.declared())
        for row_number, row in _bounded_rows(reader, max_rows=max_rows):
            counters["total_rows"] += 1
            if None in row:
                raise CellOriginInputError(
                    f"unexpected extra TSV fields at row {row_number}"
                )
            if _false_or_true(row[columns.fail], row_number):
                counters["source_failed_rows"] += 1
                continue
            counters["source_passed_rows"] += 1
            primary_base = _required_text(
                row[columns.modified_primary_base],
                "modified_primary_base",
                row_number,
            )
            if primary_base != "C":
                counters["excluded_non_c_rows"] += 1
                continue
            counters["candidate_c_rows"] += 1
            call_code = _required_text(
                row[columns.call_code], "call_code", row_number
            )
            if call_code not in {"C", "m", "h"}:
                raise CellOriginInputError(
                    f"unsupported C call code at row {row_number}"
                )
            counters[f"hard_call_{call_code.lower()}_rows"] += 1
            chromosome = _required_text(
                row[columns.chromosome], "chromosome", row_number
            )
            original_position0 = _integer(
                row[columns.position0], "position0", row_number
            )
            modification_strand = _strand(
                row[columns.modification_strand],
                "modification_strand",
                row_number,
            )
            reference_mod_strand = _strand(
                row[columns.reference_mod_strand],
                "reference_mod_strand",
                row_number,
            )
            canonical_position0 = _canonical_cpg_position(
                chromosome=chromosome,
                original_position0=original_position0,
                reference_mod_strand=reference_mod_strand,
                reference_context_provider=reference_context_provider,
                row_number=row_number,
            )
            try:
                call = ModkitCpgCallV2(
                    schema_version="cell-origin-cpg-call.v2",
                    fragment_digest=_fragment_digest(
                        row[columns.fragment_id],
                        fragment_hash_salt,
                        row_number,
                    ),
                    chromosome=chromosome,
                    original_position0=original_position0,
                    canonical_cpg_position0=canonical_position0,
                    modification_strand=modification_strand,
                    reference_mod_strand=reference_mod_strand,
                    selected_state_probability=_fraction(
                        row[columns.selected_state_probability],
                        "selected_state_probability",
                        row_number,
                    ),
                    state=(
                        CpgCallState.UNMETHYLATED
                        if call_code == "C"
                        else CpgCallState.METHYLATED
                    ),
                    policy=provenance.policy,
                )
            except ValidationError as exc:
                _model_error("generic hard call", row_number, exc)
            key = _v2_call_key(call)
            if key in seen:
                raise CellOriginInputError(
                    f"duplicate exact CpG observation at row {row_number}"
                )
            seen.add(key)
            calls.append(call)
            counters["eligible_call_rows"] += 1
            counters[
                "unmethylated_call_rows"
                if call.state == CpgCallState.UNMETHYLATED
                else "methylated_call_rows"
            ] += 1
            counters[
                "reference_plus_call_rows"
                if reference_mod_strand == Strand.PLUS
                else "reference_minus_call_rows"
            ] += 1
    return _v2_result(provenance=provenance, calls=calls, counters=counters)


def load_generic_cmh_probabilities_v1(
    source: TextSource,
    *,
    columns: GenericCmhProbabilityColumnsV1,
    provenance: ModkitInputProvenanceV2,
    fragment_hash_salt: bytes,
    reference_context_provider: ReferenceContextProvider,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ModkitInputResultV2:
    """Combine generic C/m/h probabilities before selecting a CpG state."""

    if provenance.policy != ModProbabilityPolicy.PRECALL_COMBINED_M_H:
        raise CellOriginInputError(
            "probability loader requires precall-combined provenance"
        )
    if (
        provenance.source_schema_id
        != ModkitSourceSchema.GENERIC_CMH_PROBABILITIES_V1
    ):
        raise CellOriginInputError("probability loader requires its generic schema")
    threshold = provenance.probability_threshold
    if threshold is None:
        raise CellOriginInputError("probability loader requires an explicit threshold")
    _validate_v2_common(
        fragment_hash_salt=fragment_hash_salt,
        reference_context_provider=reference_context_provider,
    )
    counters = {
        "total_rows": 0,
        "source_failed_rows": 0,
        "source_passed_rows": 0,
        "excluded_non_c_rows": 0,
        "candidate_c_rows": 0,
        "hard_call_c_rows": 0,
        "hard_call_m_rows": 0,
        "hard_call_h_rows": 0,
        "probability_input_rows": 0,
        "excluded_probability_tie_rows": 0,
        "excluded_low_confidence_rows": 0,
        "eligible_call_rows": 0,
        "unmethylated_call_rows": 0,
        "methylated_call_rows": 0,
        "reference_plus_call_rows": 0,
        "reference_minus_call_rows": 0,
    }
    calls: list[ModkitCpgCallV2] = []
    seen: set[tuple[str, str, int, Strand, Strand]] = set()
    with _open_text(source) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_declared_columns(reader.fieldnames or (), columns.declared())
        for row_number, row in _bounded_rows(reader, max_rows=max_rows):
            counters["total_rows"] += 1
            if None in row:
                raise CellOriginInputError(
                    f"unexpected extra TSV fields at row {row_number}"
                )
            if _false_or_true(row[columns.fail], row_number):
                counters["source_failed_rows"] += 1
                continue
            counters["source_passed_rows"] += 1
            primary_base = _required_text(
                row[columns.modified_primary_base],
                "modified_primary_base",
                row_number,
            )
            if primary_base != "C":
                counters["excluded_non_c_rows"] += 1
                continue
            counters["candidate_c_rows"] += 1
            counters["probability_input_rows"] += 1
            chromosome = _required_text(
                row[columns.chromosome], "chromosome", row_number
            )
            original_position0 = _integer(
                row[columns.position0], "position0", row_number
            )
            modification_strand = _strand(
                row[columns.modification_strand],
                "modification_strand",
                row_number,
            )
            reference_mod_strand = _strand(
                row[columns.reference_mod_strand],
                "reference_mod_strand",
                row_number,
            )
            canonical_position0 = _canonical_cpg_position(
                chromosome=chromosome,
                original_position0=original_position0,
                reference_mod_strand=reference_mod_strand,
                reference_context_provider=reference_context_provider,
                row_number=row_number,
            )
            canonical_probability = _fraction(
                row[columns.canonical_probability],
                "canonical_probability",
                row_number,
            )
            methyl_probability = _fraction(
                row[columns.methyl_probability],
                "methyl_probability",
                row_number,
            )
            hydroxymethyl_probability = _fraction(
                row[columns.hydroxymethyl_probability],
                "hydroxymethyl_probability",
                row_number,
            )
            total_probability = (
                canonical_probability
                + methyl_probability
                + hydroxymethyl_probability
            )
            if not math.isclose(
                total_probability,
                1.0,
                rel_tol=0.0,
                abs_tol=provenance.probability_sum_tolerance,
            ):
                raise CellOriginInputError(
                    f"C/m/h probabilities must sum to one at row {row_number}"
                )
            combined_probability = methyl_probability + hydroxymethyl_probability
            if combined_probability > 1.0:
                raise CellOriginInputError(
                    f"combined modification probability exceeds one at row {row_number}"
                )
            difference = combined_probability - canonical_probability
            if abs(difference) <= provenance.probability_tie_tolerance:
                counters["excluded_probability_tie_rows"] += 1
                continue
            selected_probability = max(
                canonical_probability, combined_probability
            )
            if selected_probability <= 0.5 or selected_probability < threshold:
                counters["excluded_low_confidence_rows"] += 1
                continue
            try:
                call = ModkitCpgCallV2(
                    schema_version="cell-origin-cpg-call.v2",
                    fragment_digest=_fragment_digest(
                        row[columns.fragment_id],
                        fragment_hash_salt,
                        row_number,
                    ),
                    chromosome=chromosome,
                    original_position0=original_position0,
                    canonical_cpg_position0=canonical_position0,
                    modification_strand=modification_strand,
                    reference_mod_strand=reference_mod_strand,
                    selected_state_probability=selected_probability,
                    state=(
                        CpgCallState.METHYLATED
                        if difference > 0.0
                        else CpgCallState.UNMETHYLATED
                    ),
                    policy=provenance.policy,
                )
            except ValidationError as exc:
                _model_error("generic C/m/h probability call", row_number, exc)
            key = _v2_call_key(call)
            if key in seen:
                raise CellOriginInputError(
                    f"duplicate exact CpG observation at row {row_number}"
                )
            seen.add(key)
            calls.append(call)
            counters["eligible_call_rows"] += 1
            counters[
                "unmethylated_call_rows"
                if call.state == CpgCallState.UNMETHYLATED
                else "methylated_call_rows"
            ] += 1
            counters[
                "reference_plus_call_rows"
                if reference_mod_strand == Strand.PLUS
                else "reference_minus_call_rows"
            ] += 1
    return _v2_result(provenance=provenance, calls=calls, counters=counters)


def load_modkit_extract_calls(
    source: TextSource,
    *,
    columns: ModkitExtractColumns,
    fragment_hash_salt: bytes,
    method: MethodDefinition = LOYFER_UXM_METHOD,
    coordinate_system: CoordinateSystem = CoordinateSystem.ZERO_BASED,
    probability_unit: FractionUnit = FractionUnit.FRACTION,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> tuple[ModkitCpgCall, ...]:
    """Load eligible C calls from a normalized modkit calls TSV.

    Rows where ``fail`` is true or ``modified_primary_base`` is not ``C`` are
    excluded. ``m`` and ``h`` call codes are both normalized to methylated;
    canonical ``C`` calls are unmethylated. Raw fragment IDs are replaced by a
    salted SHA-256 digest before model construction.
    """

    _validate_method(method, LOYFER_UXM_METHOD, "modkit calls")
    _validate_fraction_unit(probability_unit)
    if coordinate_system != CoordinateSystem.ZERO_BASED:
        raise CellOriginInputError(
            "modkit calls require explicit zero-based reference coordinates"
        )
    if not isinstance(fragment_hash_salt, bytes) or not fragment_hash_salt:
        raise CellOriginInputError("fragment_hash_salt must be nonempty bytes")
    with _open_text(source) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_declared_columns(reader.fieldnames or (), columns.declared())
        calls: list[ModkitCpgCall] = []
        seen: set[tuple[str, str, int, str]] = set()
        for row_number, row in _bounded_rows(reader, max_rows=max_rows):
            if None in row:
                raise CellOriginInputError(
                    f"unexpected extra TSV fields at row {row_number}"
                )
            if _false_or_true(row[columns.fail], row_number):
                continue
            primary_base = _required_text(
                row[columns.modified_primary_base],
                "modified_primary_base",
                row_number,
            )
            if primary_base != "C":
                continue
            call_code = _required_text(
                row[columns.call_code], "call_code", row_number
            )
            if call_code not in {"C", "m", "h"}:
                raise CellOriginInputError(
                    f"unsupported C call code at row {row_number}"
                )
            raw_fragment_id = _required_text(
                row[columns.fragment_id], "fragment_id", row_number
            )
            fragment_digest = hashlib.sha256(
                fragment_hash_salt + b"\0" + raw_fragment_id.encode("utf-8")
            ).hexdigest()
            chromosome = _required_text(
                row[columns.chromosome], "chromosome", row_number
            )
            position0 = _integer(
                row[columns.position0], "position0", row_number
            )
            strand_text = _required_text(
                row[columns.strand], "strand", row_number
            )
            try:
                strand = Strand(strand_text)
            except ValueError as exc:
                raise CellOriginInputError(
                    f"strand must be + or - at row {row_number}"
                ) from exc
            key = (fragment_digest, chromosome, position0, strand.value)
            if key in seen:
                raise CellOriginInputError(
                    f"duplicate fragment CpG locus at row {row_number}"
                )
            seen.add(key)
            try:
                calls.append(
                    ModkitCpgCall(
                        fragment_digest=fragment_digest,
                        chromosome=chromosome,
                        position0=position0,
                        strand=strand,
                        modification_code="m",
                        modified_probability=_fraction(
                            row[columns.modified_probability],
                            "modified_probability",
                            row_number,
                            unit=probability_unit,
                        ),
                        state=(
                            CpgCallState.METHYLATED
                            if call_code in {"m", "h"}
                            else CpgCallState.UNMETHYLATED
                        ),
                    )
                )
            except ValidationError as exc:
                _model_error("modkit call", row_number, exc)
        return tuple(calls)


def load_loyfer_atlas_u_matrix(
    source: TextSource,
    *,
    columns: AtlasUColumns,
    atlas_id: str,
    source_ids: Sequence[str],
    method: MethodDefinition = LOYFER_UXM_METHOD,
    expected_marker_ids: Sequence[str] | None = None,
    expected_cell_type_ids: Sequence[str] | None = None,
    fraction_unit: FractionUnit = FractionUnit.FRACTION,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> AtlasUMatrix:
    """Load a Loyfer marker-by-cell U-fraction matrix from a mapped TSV."""

    _validate_method(method, LOYFER_UXM_METHOD, "Loyfer atlas")
    _validate_fraction_unit(fraction_unit)
    if not columns.cell_type_columns:
        raise CellOriginInputError("atlas requires at least one cell type column")
    cell_type_ids = tuple(item[0] for item in columns.cell_type_columns)
    if len(cell_type_ids) != len(set(cell_type_ids)):
        raise CellOriginInputError("atlas cell type IDs must be unique")
    _validate_exact_ids(
        cell_type_ids, expected_cell_type_ids, "atlas cell type"
    )
    with _open_text(source) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_declared_columns(reader.fieldnames or (), columns.declared())
        rows: list[AtlasUMatrixRow] = []
        marker_ids: list[str] = []
        seen_marker_ids: set[str] = set()
        for row_number, row in _bounded_rows(reader, max_rows=max_rows):
            marker_id = _required_text(
                row[columns.marker_id], "marker_id", row_number
            )
            if marker_id in seen_marker_ids:
                raise CellOriginInputError(
                    f"duplicate atlas marker ID at row {row_number}"
                )
            seen_marker_ids.add(marker_id)
            marker_ids.append(marker_id)
            try:
                rows.append(
                    AtlasUMatrixRow(
                        marker_id=marker_id,
                        values=tuple(
                            AtlasUValue(
                                cell_type_id=cell_type_id,
                                u_fraction=_fraction(
                                    row[column_name],
                                    f"U fraction for {cell_type_id}",
                                    row_number,
                                    unit=fraction_unit,
                                ),
                            )
                            for cell_type_id, column_name in (
                                columns.cell_type_columns
                            )
                        ),
                    )
                )
            except ValidationError as exc:
                _model_error("atlas matrix", row_number, exc)
    if not rows:
        raise CellOriginInputError("atlas matrix must contain at least one row")
    _validate_exact_ids(marker_ids, expected_marker_ids, "atlas marker")
    try:
        return AtlasUMatrix(
            atlas_id=atlas_id,
            method=method,
            cell_type_ids=cell_type_ids,
            rows=tuple(rows),
            source_ids=tuple(source_ids),
        )
    except ValidationError as exc:
        raise CellOriginInputError(
            "atlas matrix failed schema validation"
        ) from exc


def load_marker_bed(
    source: TextSource,
    *,
    columns: MarkerBedColumns,
    atlas_id: str,
    source_ids: Sequence[str],
    method: MethodDefinition = LOYFER_UXM_METHOD,
    coordinate_system: CoordinateSystem = (
        CoordinateSystem.BED_ZERO_BASED_HALF_OPEN
    ),
    expected_marker_ids: Sequence[str] | None = None,
    expected_cell_type_ids: Sequence[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> tuple[GenomicMarker, ...]:
    """Load a headerless marker BED using an explicit zero-based mapping."""

    _validate_method(method, LOYFER_UXM_METHOD, "marker BED")
    if coordinate_system != CoordinateSystem.BED_ZERO_BASED_HALF_OPEN:
        raise CellOriginInputError(
            "marker BED requires zero-based, half-open coordinates"
        )
    declared = columns.declared()
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        for index in declared
    ):
        raise CellOriginInputError(
            "BED column mappings must be nonnegative integer indexes"
        )
    if len(declared) != len(set(declared)):
        raise CellOriginInputError("BED column mappings must be unique")
    width = len(declared)
    if set(declared) != set(range(width)):
        raise CellOriginInputError(
            "BED mapping must explicitly account for every input column"
        )
    markers: list[GenomicMarker] = []
    marker_ids: list[str] = []
    seen_marker_ids: set[str] = set()
    observed_cell_ids: list[str] = []
    with _open_text(source) as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row_number, row in _bounded_rows(reader, max_rows=max_rows):
            if not row or row[0].startswith(("#", "track")):
                continue
            if len(row) != width:
                raise CellOriginInputError(
                    f"BED row {row_number} has {len(row)} columns; "
                    f"expected {width}"
                )
            marker_id = _required_text(
                row[columns.marker_id], "marker_id", row_number
            )
            if marker_id in seen_marker_ids:
                raise CellOriginInputError(
                    f"duplicate marker ID at row {row_number}"
                )
            seen_marker_ids.add(marker_id)
            marker_ids.append(marker_id)
            target_cell_type_id = _required_text(
                row[columns.target_cell_type_id],
                "target_cell_type_id",
                row_number,
            )
            observed_cell_ids.append(target_cell_type_id)
            try:
                markers.append(
                    GenomicMarker(
                        marker_id=marker_id,
                        chromosome=_required_text(
                            row[columns.chromosome],
                            "chromosome",
                            row_number,
                        ),
                        start0=_integer(
                            row[columns.start0], "start0", row_number
                        ),
                        end0=_integer(row[columns.end0], "end0", row_number),
                        target_cell_type_id=target_cell_type_id,
                        atlas_id=atlas_id,
                        source_ids=tuple(source_ids),
                    )
                )
            except ValidationError as exc:
                _model_error("marker BED", row_number, exc)
    if not markers:
        raise CellOriginInputError("marker BED must contain at least one row")
    _validate_exact_ids(marker_ids, expected_marker_ids, "marker")
    if expected_cell_type_ids is not None:
        unknown = set(observed_cell_ids) - set(expected_cell_type_ids)
        if unknown:
            raise CellOriginInputError(
                "marker target cell IDs do not exactly match registry"
            )
    return tuple(markers)


def load_healthy_table_s8(
    records: Iterable[Mapping[str, Any]],
    *,
    columns: HealthyTableS8Columns,
    source_ids: Sequence[str],
    method: MethodDefinition = LOYFER_UXM_METHOD,
    fraction_unit: FractionUnit = FractionUnit.FRACTION,
    expected_cell_type_ids: Sequence[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> HealthyTableS8:
    """Load normalized CSV/XLSX-compatible healthy Table S8 records."""

    _validate_method(method, LOYFER_UXM_METHOD, "Table S8")
    _validate_fraction_unit(fraction_unit)
    if not columns.sample_fraction_columns:
        raise CellOriginInputError(
            "Table S8 requires at least one healthy sample column"
        )
    sample_ids = tuple(item[0] for item in columns.sample_fraction_columns)
    if len(sample_ids) != len(set(sample_ids)):
        raise CellOriginInputError("healthy sample IDs must be unique")
    declared = columns.declared()
    if len(declared) != len(set(declared)):
        raise CellOriginInputError("Table S8 column mappings must be unique")
    rows: list[HealthyTableS8Row] = []
    cell_type_ids: list[str] = []
    seen_cell_type_ids: set[str] = set()
    for row_number, record in _bounded_rows(records, max_rows=max_rows):
        if not isinstance(record, Mapping):
            raise CellOriginInputError(
                f"Table S8 row {row_number} must be a mapping"
            )
        actual = tuple(record.keys())
        if any(not isinstance(key, str) for key in actual):
            raise CellOriginInputError(
                f"Table S8 row {row_number} has a non-text column name"
            )
        _validate_declared_columns(actual, declared)
        cell_type_id = _required_text(
            record[columns.cell_type_id], "cell_type_id", row_number
        )
        if cell_type_id in seen_cell_type_ids:
            raise CellOriginInputError(
                f"duplicate healthy cell type ID at row {row_number}"
            )
        seen_cell_type_ids.add(cell_type_id)
        cell_type_ids.append(cell_type_id)
        try:
            sample_fractions = tuple(
                HealthySampleFraction(
                    sample_id=sample_id,
                    fraction=_fraction(
                        record[column_name],
                        f"healthy fraction for {sample_id}",
                        row_number,
                        unit=fraction_unit,
                    ),
                )
                for sample_id, column_name in columns.sample_fraction_columns
            )
            values = tuple(item.fraction for item in sample_fractions)
            rows.append(
                HealthyTableS8Row(
                    cell_type_id=cell_type_id,
                    sample_fractions=sample_fractions,
                    min_fraction=min(values),
                    max_fraction=max(values),
                )
            )
        except ValidationError as exc:
            _model_error("Table S8", row_number, exc)
    if not rows:
        raise CellOriginInputError("Table S8 must contain at least one row")
    _validate_exact_ids(
        cell_type_ids, expected_cell_type_ids, "healthy cell type"
    )
    try:
        return HealthyTableS8(
            method=method,
            source_ids=tuple(source_ids),
            sample_ids=sample_ids,
            rows=tuple(rows),
        )
    except ValidationError as exc:
        raise CellOriginInputError(
            "Table S8 failed schema validation"
        ) from exc


__all__ = [
    "AtlasUColumns",
    "CellOriginInputError",
    "CoordinateSystem",
    "FractionUnit",
    "GenericCmhProbabilityColumnsV1",
    "GenericHardCallColumnsV2",
    "HealthySampleFraction",
    "HealthyTableS8",
    "HealthyTableS8Columns",
    "HealthyTableS8Row",
    "MarkerBedColumns",
    "ModkitExtractColumns",
    "ReferenceContextProvider",
    "load_generic_cmh_probabilities_v1",
    "load_generic_hard_call_cpg_v2",
    "load_healthy_table_s8",
    "load_loyfer_atlas_u_matrix",
    "load_marker_bed",
    "load_modkit_extract_calls",
]
