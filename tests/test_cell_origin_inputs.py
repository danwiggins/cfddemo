"""Tests for bounded cell-origin scientific input loaders."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest

from evidence_inspector.cell_origin_inputs import (
    AtlasUColumns,
    CellOriginInputError,
    CoordinateSystem,
    FractionUnit,
    GenericCmhProbabilityColumnsV1,
    GenericHardCallColumnsV2,
    HealthyTableS8Columns,
    MarkerBedColumns,
    ModkitExtractColumns,
    load_healthy_table_s8,
    load_generic_cmh_probabilities_v1,
    load_generic_hard_call_cpg_v2,
    load_loyfer_atlas_u_matrix,
    load_marker_bed,
    load_modkit_extract_calls,
)
from evidence_inspector.cell_origin_models import (
    CpgCallState,
    KATSMAN_METHATLAS_METHOD,
    LOYFER_UXM_METHOD,
    ModProbabilityPolicy,
    ModkitInputProvenanceV2,
    ModkitSourceSchema,
)

MODKIT_COLUMNS = ModkitExtractColumns(
    fragment_id="read_id",
    chromosome="chrom",
    position0="ref_position",
    strand="mod_strand",
    modified_primary_base="modified_primary_base",
    call_code="call_code",
    modified_probability="modified_probability",
    fail="fail",
)
MODKIT_HEADER = (
    "read_id\tchrom\tref_position\tmod_strand\tmodified_primary_base\t"
    "call_code\tmodified_probability\tfail\n"
)
HARD_V2_COLUMNS = GenericHardCallColumnsV2(
    fragment_id="read_id",
    chromosome="chrom",
    position0="ref_position",
    modification_strand="mod_strand",
    reference_mod_strand="ref_mod_strand",
    modified_primary_base="modified_primary_base",
    call_code="call_code",
    selected_state_probability="call_probability",
    fail="fail",
)
HARD_V2_HEADER = "\t".join(HARD_V2_COLUMNS.declared()) + "\n"
PROBABILITY_V1_COLUMNS = GenericCmhProbabilityColumnsV1(
    fragment_id="read_id",
    chromosome="chrom",
    position0="ref_position",
    modification_strand="mod_strand",
    reference_mod_strand="ref_mod_strand",
    modified_primary_base="modified_primary_base",
    canonical_probability="p_c",
    methyl_probability="p_m",
    hydroxymethyl_probability="p_h",
    fail="fail",
)
PROBABILITY_V1_HEADER = "\t".join(PROBABILITY_V1_COLUMNS.declared()) + "\n"
ATLAS_COLUMNS = AtlasUColumns(
    marker_id="marker",
    cell_type_columns=(("immune", "Immune"), ("liver", "Liver")),
    ignored=("description",),
)
BED_COLUMNS = MarkerBedColumns(
    chromosome=0,
    start0=1,
    end0=2,
    marker_id=3,
    target_cell_type_id=4,
)
HEALTHY_COLUMNS = HealthyTableS8Columns(
    cell_type_id="cell_type",
    sample_fraction_columns=(("healthy.1", "H1"), ("healthy.2", "H2")),
)


def hard_v2_provenance() -> ModkitInputProvenanceV2:
    return ModkitInputProvenanceV2(
        source_schema_id=ModkitSourceSchema.GENERIC_HARD_CALL_CPG_V2,
        source_schema_version="2",
        source_tool_id="unknown",
        source_tool_version="unknown",
        source_model_id="unknown",
        source_model_version="unknown",
        policy=ModProbabilityPolicy.HARD_CALL_COLLAPSED_M_H,
        probability_threshold=None,
        probability_threshold_source="source_unknown",
        reference_id="reference.synthetic",
        reference_sha256="a" * 64,
        reference_context_provider_id="provider.synthetic.v1",
    )


def probability_v1_provenance(
    threshold: float = 0.5,
) -> ModkitInputProvenanceV2:
    return ModkitInputProvenanceV2(
        source_schema_id=ModkitSourceSchema.GENERIC_CMH_PROBABILITIES_V1,
        source_schema_version="1",
        source_tool_id="unknown",
        source_tool_version="unknown",
        source_model_id="unknown",
        source_model_version="unknown",
        policy=ModProbabilityPolicy.PRECALL_COMBINED_M_H,
        probability_threshold=threshold,
        probability_threshold_source="adapter_explicit",
        reference_id="reference.synthetic",
        reference_sha256="a" * 64,
        reference_context_provider_id="provider.synthetic.v1",
    )


def cpg_provider(chromosome: str, start0: int, end0: int) -> str:
    assert chromosome == "chr1"
    assert end0 == start0 + 2
    return "CG"


def test_modkit_loader_filters_and_hashes_raw_fragment_ids() -> None:
    text = MODKIT_HEADER + (
        "secret-read-1\tchr1\t100\t+\tC\tm\t0.91\tfalse\n"
        "secret-read-1\tchr1\t101\t+\tC\th\t0.82\tfalse\n"
        "secret-read-2\tchr2\t200\t-\tC\tC\t0.04\tFalse\n"
        "failed-read\tchr1\t300\t+\tC\tm\t0.99\ttrue\n"
        "adenine-read\tchr1\t301\t+\tA\ta\t0.75\tfalse\n"
    )

    calls = load_modkit_extract_calls(
        io.StringIO(text),
        columns=MODKIT_COLUMNS,
        fragment_hash_salt=b"test-only-salt",
    )

    assert [call.state for call in calls] == [
        CpgCallState.METHYLATED,
        CpgCallState.METHYLATED,
        CpgCallState.UNMETHYLATED,
    ]
    assert calls[0].modification_code == "m"
    assert calls[0].fragment_digest == calls[1].fragment_digest
    assert calls[0].fragment_digest != calls[2].fragment_digest
    assert "secret-read" not in repr(calls)
    assert all(len(call.fragment_digest) == 64 for call in calls)


def test_v2_precombines_m_and_h_before_state_selection() -> None:
    hard_text = HARD_V2_HEADER + (
        "secret-read\tchr1\t100\t+\t+\tC\tC\t0.45\tfalse\n"
    )
    probability_text = PROBABILITY_V1_HEADER + (
        "secret-read\tchr1\t100\t+\t+\tC\t0.45\t0.40\t0.15\tfalse\n"
    )

    hard = load_generic_hard_call_cpg_v2(
        io.StringIO(hard_text),
        columns=HARD_V2_COLUMNS,
        provenance=hard_v2_provenance(),
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )
    combined = load_generic_cmh_probabilities_v1(
        io.StringIO(probability_text),
        columns=PROBABILITY_V1_COLUMNS,
        provenance=probability_v1_provenance(),
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )

    assert hard.calls[0].state == CpgCallState.UNMETHYLATED
    assert hard.calls[0].selected_state_probability == 0.45
    assert hard.calls[0].policy == ModProbabilityPolicy.HARD_CALL_COLLAPSED_M_H
    assert combined.calls[0].state == CpgCallState.METHYLATED
    assert combined.calls[0].selected_state_probability == pytest.approx(0.55)
    assert combined.calls[0].policy == ModProbabilityPolicy.PRECALL_COMBINED_M_H
    assert "secret-read" not in repr(hard)
    assert "secret-read" not in repr(combined)


def test_v2_probability_ledger_reconciles_every_terminal_stage() -> None:
    text = PROBABILITY_V1_HEADER + (
        "failed\tchr1\t10\t+\t+\tC\t0.7\t0.2\t0.1\ttrue\n"
        "adenine\tchr1\t20\t+\t+\tA\t0.7\t0.2\t0.1\tfalse\n"
        "tie\tchr1\t30\t+\t+\tC\t0.5\t0.4\t0.1\tfalse\n"
        "low\tchr1\t40\t+\t+\tC\t0.51\t0.30\t0.19\tfalse\n"
        "plus\tchr1\t100\t+\t+\tC\t0.7\t0.2\t0.1\tfalse\n"
        "minus\tchr1\t201\t+\t-\tC\t0.2\t0.7\t0.1\tfalse\n"
    )

    result = load_generic_cmh_probabilities_v1(
        io.StringIO(text),
        columns=PROBABILITY_V1_COLUMNS,
        provenance=probability_v1_provenance(threshold=0.6),
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )

    ledger = result.ledger
    assert ledger.total_rows == 6
    assert ledger.source_failed_rows == 1
    assert ledger.source_passed_rows == 5
    assert ledger.excluded_non_c_rows == 1
    assert ledger.candidate_c_rows == 4
    assert ledger.probability_input_rows == 4
    assert ledger.excluded_probability_tie_rows == 1
    assert ledger.excluded_low_confidence_rows == 1
    assert ledger.eligible_call_rows == 2
    assert ledger.unmethylated_call_rows == 1
    assert ledger.methylated_call_rows == 1
    assert ledger.reference_plus_call_rows == 1
    assert ledger.reference_minus_call_rows == 1
    assert [call.canonical_cpg_position0 for call in result.calls] == [100, 200]


def test_v2_hard_call_ledger_preserves_source_call_codes() -> None:
    text = HARD_V2_HEADER + (
        "c\tchr1\t100\t+\t+\tC\tC\t0.45\tfalse\n"
        "m\tchr1\t200\t+\t+\tC\tm\t0.80\tfalse\n"
        "h\tchr1\t301\t+\t-\tC\th\t0.70\tfalse\n"
    )

    result = load_generic_hard_call_cpg_v2(
        io.StringIO(text),
        columns=HARD_V2_COLUMNS,
        provenance=hard_v2_provenance(),
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )

    assert result.ledger.hard_call_c_rows == 1
    assert result.ledger.hard_call_m_rows == 1
    assert result.ledger.hard_call_h_rows == 1
    assert result.ledger.candidate_c_rows == 3
    assert result.ledger.eligible_call_rows == 3
    assert [call.state for call in result.calls] == [
        CpgCallState.UNMETHYLATED,
        CpgCallState.METHYLATED,
        CpgCallState.METHYLATED,
    ]


def test_v2_canonicalizes_both_reference_strands_to_one_cpg_dyad() -> None:
    text = HARD_V2_HEADER + (
        "same-fragment\tchr1\t100\t+\t+\tC\tm\t0.9\tfalse\n"
        "same-fragment\tchr1\t101\t+\t-\tC\tm\t0.9\tfalse\n"
    )

    result = load_generic_hard_call_cpg_v2(
        io.StringIO(text),
        columns=HARD_V2_COLUMNS,
        provenance=hard_v2_provenance(),
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )

    assert [call.canonical_cpg_position0 for call in result.calls] == [100, 100]
    assert len(result.calls) == 2
    assert result.calls[0].reference_mod_strand.value == "+"
    assert result.calls[1].reference_mod_strand.value == "-"


@pytest.mark.parametrize(
    ("row", "message", "provider"),
    [
        (
            "r1\tchr1\t0\t+\t-\tC\tm\t0.9\tfalse\n",
            "underflows",
            cpg_provider,
        ),
        (
            "r1\tchr1\t100\t+\t+\tC\tm\t0.9\tfalse\n",
            "not a CpG dyad",
            lambda chromosome, start0, end0: "CA",
        ),
    ],
)
def test_v2_reference_validation_fails_closed(
    row: str,
    message: str,
    provider: object,
) -> None:
    with pytest.raises(CellOriginInputError, match=message):
        load_generic_hard_call_cpg_v2(
            io.StringIO(HARD_V2_HEADER + row),
            columns=HARD_V2_COLUMNS,
            provenance=hard_v2_provenance(),
            fragment_hash_salt=b"test-salt",
            reference_context_provider=provider,  # type: ignore[arg-type]
        )


def test_v2_exact_duplicates_fail_closed_but_opposite_strands_do_not() -> None:
    duplicate = (
        "same\tchr1\t100\t+\t+\tC\tm\t0.9\tfalse\n"
        "same\tchr1\t100\t+\t+\tC\tm\t0.8\tfalse\n"
    )

    with pytest.raises(CellOriginInputError, match="duplicate exact"):
        load_generic_hard_call_cpg_v2(
            io.StringIO(HARD_V2_HEADER + duplicate),
            columns=HARD_V2_COLUMNS,
            provenance=hard_v2_provenance(),
            fragment_hash_salt=b"test-salt",
            reference_context_provider=cpg_provider,
        )


@pytest.mark.parametrize(
    ("probabilities", "message"),
    [
        ("0.4\t0.3\t0.2", "sum to one"),
        ("0.4\t0.7\t-0.1", r"within \[0, 1\]"),
        ("nan\t0.5\t0.5", "finite"),
    ],
)
def test_v2_probability_rows_fail_closed_on_invalid_values(
    probabilities: str,
    message: str,
) -> None:
    row = f"r1\tchr1\t100\t+\t+\tC\t{probabilities}\tfalse\n"

    with pytest.raises(CellOriginInputError, match=message):
        load_generic_cmh_probabilities_v1(
            io.StringIO(PROBABILITY_V1_HEADER + row),
            columns=PROBABILITY_V1_COLUMNS,
            provenance=probability_v1_provenance(),
            fragment_hash_salt=b"test-salt",
            reference_context_provider=cpg_provider,
        )


def test_v2_zero_threshold_and_deterministic_repeat_are_explicit() -> None:
    text = PROBABILITY_V1_HEADER + (
        "r1\tchr1\t100\t+\t+\tC\t0.499999\t0.400001\t0.1\tfalse\n"
    )
    provenance = probability_v1_provenance(threshold=0.0)

    first = load_generic_cmh_probabilities_v1(
        io.StringIO(text),
        columns=PROBABILITY_V1_COLUMNS,
        provenance=provenance,
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )
    repeated = load_generic_cmh_probabilities_v1(
        io.StringIO(text),
        columns=PROBABILITY_V1_COLUMNS,
        provenance=provenance,
        fragment_hash_salt=b"test-salt",
        reference_context_provider=cpg_provider,
    )

    assert first == repeated
    assert first.calls[0].state == CpgCallState.METHYLATED
    assert first.provenance.probability_threshold == 0.0
    assert first.provenance.probability_tie_tolerance == 0.0
    assert first.provenance.probability_sum_tolerance == 1e-6


def test_modkit_loader_requires_explicit_coordinate_and_probability_units() -> None:
    text = MODKIT_HEADER + "r1\tchr1\t0\t+\tC\tm\t91\tfalse\n"
    calls = load_modkit_extract_calls(
        io.StringIO(text),
        columns=MODKIT_COLUMNS,
        fragment_hash_salt=b"salt",
        probability_unit=FractionUnit.PERCENT,
    )
    assert calls[0].modified_probability == 0.91

    with pytest.raises(CellOriginInputError, match="zero-based"):
        load_modkit_extract_calls(
            io.StringIO(text),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
            coordinate_system=CoordinateSystem.BED_ZERO_BASED_HALF_OPEN,
            probability_unit=FractionUnit.PERCENT,
        )
    with pytest.raises(CellOriginInputError, match="fraction unit"):
        load_modkit_extract_calls(
            io.StringIO(text),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
            probability_unit="phred",  # type: ignore[arg-type]
        )
    with pytest.raises(CellOriginInputError, match="method identity"):
        load_modkit_extract_calls(
            io.StringIO(text),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
            method=KATSMAN_METHATLAS_METHOD,
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("r1\tchr1\t1\t+\tC\tx\t0.1\tfalse\n", "unsupported C call"),
        ("r1\tchr1\t1\t+\tC\tm\tnan\tfalse\n", "finite"),
        ("r1\tchr1\t1\t+\tC\tm\t1.1\tfalse\n", r"within \[0, 1\]"),
        ("r1\tchr1\t1\t+\tC\tm\t0.1\tunknown\n", "exactly true or false"),
        ("r1\t1\t1\t+\tC\tm\t0.1\tfalse\n", "schema validation"),
    ],
)
def test_modkit_loader_rejects_invalid_eligible_rows(
    row: str, message: str
) -> None:
    with pytest.raises(CellOriginInputError, match=message):
        load_modkit_extract_calls(
            io.StringIO(MODKIT_HEADER + row),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
        )


def test_modkit_loader_rejects_unknown_columns_duplicates_and_row_overflow() -> None:
    with pytest.raises(CellOriginInputError, match="unknown columns: sample"):
        load_modkit_extract_calls(
            io.StringIO(MODKIT_HEADER.rstrip("\n") + "\tsample\n"),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
        )

    duplicate = (
        MODKIT_HEADER
        + "r1\tchr1\t1\t+\tC\tm\t0.9\tfalse\n"
        + "r1\tchr1\t1\t+\tC\th\t0.8\tfalse\n"
    )
    with pytest.raises(CellOriginInputError, match="duplicate fragment CpG"):
        load_modkit_extract_calls(
            io.StringIO(duplicate),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
        )

    two_rows = (
        MODKIT_HEADER
        + "r1\tchr1\t1\t+\tC\tm\t0.9\tfalse\n"
        + "r2\tchr1\t2\t+\tC\tm\t0.8\tfalse\n"
    )
    with pytest.raises(CellOriginInputError, match="row cap"):
        load_modkit_extract_calls(
            io.StringIO(two_rows),
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
            max_rows=1,
        )


def test_loyfer_atlas_loader_uses_exact_mapped_ids_and_percent_units() -> None:
    text = (
        "marker\tImmune\tLiver\tdescription\n"
        "marker.immune.1\t80\t20\tsynthetic\n"
        "marker.liver.1\t20\t80\tsynthetic\n"
    )
    matrix = load_loyfer_atlas_u_matrix(
        io.StringIO(text),
        columns=ATLAS_COLUMNS,
        atlas_id="atlas.synthetic-loyfer.v1",
        source_ids=("source.synthetic-atlas",),
        expected_marker_ids=("marker.immune.1", "marker.liver.1"),
        expected_cell_type_ids=("immune", "liver"),
        fraction_unit=FractionUnit.PERCENT,
    )

    assert matrix.method == LOYFER_UXM_METHOD
    assert matrix.cell_type_ids == ("immune", "liver")
    assert matrix.rows[0].values[0].u_fraction == 0.8


def test_loyfer_atlas_loader_fails_on_schema_ids_and_method_drift() -> None:
    valid = "marker\tImmune\tLiver\tdescription\nm1\t0.8\t0.2\tx\n"
    with pytest.raises(CellOriginInputError, match="unknown columns"):
        load_loyfer_atlas_u_matrix(
            io.StringIO(valid.replace("description", "unexpected")),
            columns=ATLAS_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )
    with pytest.raises(CellOriginInputError, match="do not exactly match"):
        load_loyfer_atlas_u_matrix(
            io.StringIO(valid),
            columns=ATLAS_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
            expected_marker_ids=("M1",),
        )
    with pytest.raises(CellOriginInputError, match="method identity"):
        load_loyfer_atlas_u_matrix(
            io.StringIO(valid),
            columns=ATLAS_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
            method=KATSMAN_METHATLAS_METHOD,
        )


def test_loyfer_atlas_loader_rejects_duplicate_markers_and_row_cap() -> None:
    duplicate = (
        "marker\tImmune\tLiver\tdescription\n"
        "m1\t0.8\t0.2\tx\n"
        "m1\t0.7\t0.3\ty\n"
    )
    with pytest.raises(CellOriginInputError, match="duplicate atlas marker"):
        load_loyfer_atlas_u_matrix(
            io.StringIO(duplicate),
            columns=ATLAS_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )
    with pytest.raises(CellOriginInputError, match="row cap"):
        load_loyfer_atlas_u_matrix(
            io.StringIO(duplicate.replace("m1\t0.7", "m2\t0.7")),
            columns=ATLAS_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
            max_rows=1,
        )


def test_marker_bed_loader_validates_half_open_coordinates_and_registry() -> None:
    text = (
        "chr1\t100\t200\tmarker.immune.1\timmune\n"
        "chr2\t300\t400\tmarker.liver.1\tliver\n"
    )
    markers = load_marker_bed(
        io.StringIO(text),
        columns=BED_COLUMNS,
        atlas_id="atlas.synthetic-loyfer.v1",
        source_ids=("source.synthetic-atlas",),
        expected_marker_ids=("marker.immune.1", "marker.liver.1"),
        expected_cell_type_ids=("immune", "liver"),
    )

    assert markers[0].start0 == 100
    assert markers[0].end0 == 200
    assert markers[1].target_cell_type_id == "liver"


def test_marker_bed_loader_rejects_bad_coordinates_width_and_duplicates() -> None:
    with pytest.raises(CellOriginInputError, match="schema validation"):
        load_marker_bed(
            io.StringIO("chr1\t100\t100\tm1\timmune\n"),
            columns=BED_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )
    with pytest.raises(CellOriginInputError, match="expected 5"):
        load_marker_bed(
            io.StringIO("chr1\t100\t200\tm1\timmune\textra\n"),
            columns=BED_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )
    with pytest.raises(CellOriginInputError, match="duplicate marker"):
        load_marker_bed(
            io.StringIO(
                "chr1\t100\t200\tm1\timmune\n"
                "chr2\t300\t400\tm1\tliver\n"
            ),
            columns=BED_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )


def test_marker_bed_mapping_must_account_for_every_column() -> None:
    incomplete = replace(BED_COLUMNS, ignored=(6,))
    with pytest.raises(CellOriginInputError, match="every input column"):
        load_marker_bed(
            io.StringIO("chr1\t100\t200\tm1\timmune\tx\ty\n"),
            columns=incomplete,
            atlas_id="atlas.test",
            source_ids=("source.test",),
        )
    with pytest.raises(CellOriginInputError, match="half-open"):
        load_marker_bed(
            io.StringIO("chr1\t100\t200\tm1\timmune\n"),
            columns=BED_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
            coordinate_system=CoordinateSystem.ZERO_BASED,
        )
    with pytest.raises(CellOriginInputError, match="method identity"):
        load_marker_bed(
            io.StringIO("chr1\t100\t200\tm1\timmune\n"),
            columns=BED_COLUMNS,
            atlas_id="atlas.test",
            source_ids=("source.test",),
            method=KATSMAN_METHATLAS_METHOD,
        )


def test_healthy_table_s8_loads_csv_or_xlsx_compatible_records() -> None:
    records = [
        {"cell_type": "immune", "H1": "10", "H2": 20},
        {"cell_type": "liver", "H1": 30.0, "H2": "40"},
    ]
    table = load_healthy_table_s8(
        records,
        columns=HEALTHY_COLUMNS,
        source_ids=("source.loyfer-table-s8",),
        fraction_unit=FractionUnit.PERCENT,
        expected_cell_type_ids=("immune", "liver"),
    )

    assert table.method == LOYFER_UXM_METHOD
    assert table.sample_ids == ("healthy.1", "healthy.2")
    assert table.rows[0].min_fraction == 0.1
    assert table.rows[0].max_fraction == 0.2


def test_healthy_table_s8_rejects_unknown_columns_duplicates_and_bad_values() -> None:
    with pytest.raises(CellOriginInputError, match="unknown columns"):
        load_healthy_table_s8(
            [{"cell_type": "immune", "H1": 0.1, "H2": 0.2, "H3": 0.3}],
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
        )
    with pytest.raises(CellOriginInputError, match="duplicate healthy cell"):
        load_healthy_table_s8(
            [
                {"cell_type": "immune", "H1": 0.1, "H2": 0.2},
                {"cell_type": "immune", "H1": 0.2, "H2": 0.3},
            ],
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
        )
    with pytest.raises(CellOriginInputError, match="finite"):
        load_healthy_table_s8(
            [{"cell_type": "immune", "H1": float("nan"), "H2": 0.2}],
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
        )
    with pytest.raises(CellOriginInputError, match=r"within \[0, 1\]"):
        load_healthy_table_s8(
            [{"cell_type": "immune", "H1": 1.1, "H2": 0.2}],
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
        )


def test_healthy_table_s8_rejects_method_id_and_row_cap_mismatches() -> None:
    records = [
        {"cell_type": "immune", "H1": 0.1, "H2": 0.2},
        {"cell_type": "liver", "H1": 0.3, "H2": 0.4},
    ]
    with pytest.raises(CellOriginInputError, match="method identity"):
        load_healthy_table_s8(
            records,
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
            method=KATSMAN_METHATLAS_METHOD,
        )
    with pytest.raises(CellOriginInputError, match="do not exactly match"):
        load_healthy_table_s8(
            records,
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
            expected_cell_type_ids=("Immune", "liver"),
        )
    with pytest.raises(CellOriginInputError, match="row cap"):
        load_healthy_table_s8(
            records,
            columns=HEALTHY_COLUMNS,
            source_ids=("source.s8",),
            max_rows=1,
        )


def test_errors_and_outputs_do_not_expose_paths_or_read_ids() -> None:
    with pytest.raises(CellOriginInputError) as error:
        load_modkit_extract_calls(
            "/private/nonexistent/secret-sample.tsv",
            columns=MODKIT_COLUMNS,
            fragment_hash_salt=b"salt",
        )
    assert "secret-sample" not in str(error.value)
    assert "/private/" not in str(error.value)

    text = MODKIT_HEADER + "private-read-name\tchr1\t1\t+\tC\tm\t0.9\tfalse\n"
    calls = load_modkit_extract_calls(
        io.StringIO(text),
        columns=MODKIT_COLUMNS,
        fragment_hash_salt=b"salt",
    )
    assert "private-read-name" not in repr(calls)
