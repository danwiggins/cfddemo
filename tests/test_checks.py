"""Deterministic numerical and source-review check tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_inspector.case_bundle import build_synthetic_case
from evidence_inspector.checks import (
    CheckInputError,
    compare_reference_ranges,
    load_prepared_lengths,
    read_length_summary,
    source_review,
    summarize_read_lengths,
)
from evidence_inspector.models import SelectionParameters, VerificationLevel
from evidence_inspector.preparation import (
    LENGTHS_FILE_NAME,
    MANIFEST_FILE_NAME,
    prepare_length_artifact,
)


class FakeRecord:
    is_secondary = False
    is_supplementary = False

    def __init__(self, name: str, length: int) -> None:
        self.query_name = name
        self.query_sequence = "A" * length


def test_read_length_summary_matches_hand_counted_contract_and_tie_rule() -> None:
    values = summarize_read_lengths([100, 150, 151, 151, 167, 167, 167, 1001])

    assert values.valid_read_count == 8
    assert values.mode_bp == 167
    assert values.median_bp == 159
    assert values.fraction_100_150 == 2 / 8
    assert values.fraction_gt_1000 == 1 / 8
    assert sum(item.count for item in values.bins) == 7
    assert values.overflow_count == 1
    assert summarize_read_lengths([9, 10]).mode_bp == 9

    with pytest.raises(CheckInputError, match="at least one"):
        summarize_read_lengths([])
    with pytest.raises(CheckInputError, match="positive integer"):
        summarize_read_lengths([100, 0])
    with pytest.raises(CheckInputError, match="positive integer"):
        summarize_read_lengths([True])


def test_read_length_check_verifies_prepared_bundle_and_provenance(
    tmp_path: Path,
) -> None:
    private_input = tmp_path / "secret-input.bam"
    private_input.write_bytes(b"injected")
    lengths = [100, 150, 151, 151, 167, 167, 167, 1001]
    manifest = prepare_length_artifact(
        [private_input],
        tmp_path / "bundle",
        record_source=lambda _: iter(
            FakeRecord(f"private-read-{index}", length)
            for index, length in enumerate(lengths)
        ),
        selection_parameters=SelectionParameters(
            ordering_rule="synthetic complete input",
            max_accepted_reads=100,
            max_inspected_records=100,
            max_elapsed_seconds=60,
            max_serialized_artifact_bytes=2_097_152,
        ),
    )
    result = read_length_summary(
        tmp_path / "bundle" / LENGTHS_FILE_NAME,
        artifact_id=manifest.artifact.id,
        preparation_manifest=tmp_path / "bundle" / MANIFEST_FILE_NAME,
        source_ids=("source.synthetic-length",),
    )

    assert result.values["mode_bp"] == 167
    assert result.verification_level == VerificationLevel.RECOMPUTED
    assert result.provenance.artifact_digests[manifest.artifact.id] == (
        manifest.artifact.sha256
    )
    assert result.denominator == (
        "accepted unique positive-length primary reads (n=8)"
    )
    serialized = result.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "private-read" not in serialized
    assert any("partial collection" in item for item in result.limitations)
    assert any("Unverified sample linkage" in item for item in result.limitations)


def test_prepared_length_loader_rejects_tampering_noncanonical_and_bad_values(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "lengths.json"
    artifact.write_text("[1,2,3]")
    _, digest = load_prepared_lengths(artifact)

    with pytest.raises(CheckInputError, match="digest mismatch"):
        load_prepared_lengths(artifact, expected_sha256="0" * 64)
    artifact.write_text("[1, 2, 3]")
    with pytest.raises(CheckInputError, match="canonical"):
        load_prepared_lengths(artifact)
    artifact.write_text("[1,true]")
    with pytest.raises(CheckInputError, match="invalid query length"):
        load_prepared_lengths(artifact)
    artifact.write_text("[]")
    with pytest.raises(CheckInputError, match="no valid reads"):
        load_prepared_lengths(artifact)
    assert digest


def _range_tables():
    samples = [
        {"cell_type_id": "root", "parent_id": None, "is_leaf": False, "fraction": 0.5},
        {"cell_type_id": "below", "parent_id": "root", "is_leaf": True, "fraction": 0.1},
        {"cell_type_id": "at_min", "parent_id": "root", "is_leaf": True, "fraction": 0.2},
        {"cell_type_id": "at_max", "parent_id": "root", "is_leaf": True, "fraction": 0.3},
        {"cell_type_id": "above", "parent_id": "root", "is_leaf": True, "fraction": 0.5},
    ]
    bounds = {
        "root": (0.5, 0.5),
        "below": (0.2, 0.4),
        "at_min": (0.2, 0.3),
        "at_max": (0.2, 0.3),
        "above": (0.2, 0.4),
    }
    references = [
        {
            "cell_type_id": cell_id,
            "min_fraction": minimum,
            "max_fraction": maximum,
            "cohort_id": "cohort",
            "source_id": "source.synthetic-table",
            "assay": "synthetic assay",
        }
        for cell_id, (minimum, maximum) in bounds.items()
    ]
    return samples, references


def test_reference_ranges_are_fractional_inclusive_and_never_aggregated() -> None:
    samples, references = _range_tables()
    result = compare_reference_ranges(
        samples,
        references,
        sample_artifact_id="artifact.sample",
        reference_artifact_id="artifact.reference",
        known_source_ids=("source.synthetic-table",),
    )
    rows = {row["cell_type_id"]: row for row in result.values["rows"]}

    assert rows["below"]["classification"] == "below"
    assert rows["at_min"]["classification"] == "within"
    assert rows["at_max"]["classification"] == "within"
    assert rows["above"]["classification"] == "above"
    assert rows["root"]["fraction"] == 0.5
    assert rows["root"]["classification"] == "within"
    assert result.verification_level == VerificationLevel.RECOMPUTED
    assert result.units["rows"] == "canonical fractions from 0 to 1"
    assert "no parent-child aggregation" in result.filters


def test_reference_ranges_accept_valid_csv_fractions(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    reference = tmp_path / "reference.csv"
    sample.write_text(
        "cell_type_id,parent_id,is_leaf,fraction\n"
        "root,,false,0.4\n"
        "leaf,root,true,0.2\n"
    )
    reference.write_text(
        "cell_type_id,min_fraction,max_fraction,cohort_id,source_id,assay\n"
        "root,0.3,0.5,cohort,source.synthetic-table,assay\n"
        "leaf,0.2,0.3,cohort,source.synthetic-table,assay\n"
    )
    result = compare_reference_ranges(
        sample,
        reference,
        sample_artifact_id="artifact.sample",
        reference_artifact_id="artifact.reference",
        known_source_ids=("source.synthetic-table",),
    )
    assert [row["classification"] for row in result.values["rows"]] == [
        "within",
        "within",
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("percent", "percent strings"),
        ("nan", "finite fraction"),
        ("duplicate", "duplicate sample"),
        ("unmatched", "sets must match"),
        ("reversed", "minimum cannot exceed"),
        ("unknown_source", "unknown reference source"),
        ("missing_parent", "missing parent"),
        ("leaf_parent", "leaf cell type has children"),
        ("cycle", "cycle"),
    ],
)
def test_reference_ranges_fail_closed_on_malformed_inputs(
    mutation: str, message: str
) -> None:
    samples, references = _range_tables()
    if mutation == "percent":
        samples[0]["fraction"] = "50%"
    elif mutation == "nan":
        references[0]["min_fraction"] = float("nan")
    elif mutation == "duplicate":
        samples.append(dict(samples[0]))
    elif mutation == "unmatched":
        references.pop()
    elif mutation == "reversed":
        references[0]["min_fraction"] = 0.6
    elif mutation == "unknown_source":
        references[0]["source_id"] = "source.unknown"
    elif mutation == "missing_parent":
        samples[1]["parent_id"] = "missing"
    elif mutation == "leaf_parent":
        samples[0]["is_leaf"] = True
    elif mutation == "cycle":
        samples[0]["parent_id"] = "below"
    else:
        raise AssertionError("unhandled mutation")

    with pytest.raises(CheckInputError, match=message):
        compare_reference_ranges(
            samples,
            references,
            sample_artifact_id="artifact.sample",
            reference_artifact_id="artifact.reference",
            known_source_ids=("source.synthetic-table",),
        )


def test_source_review_resolves_explicit_ids_only_and_remains_reported() -> None:
    case = build_synthetic_case()
    result = source_review(
        case,
        (
            "source.synthetic-method-current",
            "source.synthetic-method-instructions",
        ),
    )

    assert result.values["reviewed_source_ids"] == [
        "source.synthetic-method-current",
        "source.synthetic-method-instructions",
    ]
    assert result.verification_level == VerificationLevel.REPORTED
    assert result.provenance.artifact_ids == ()
    assert any("not biological truth" in item for item in result.limitations)
    with pytest.raises(KeyError, match="unknown source ID"):
        source_review(case, ("current method",))
    with pytest.raises(ValueError, match="unique"):
        source_review(
            case,
            ("source.synthetic-method-current", "source.synthetic-method-current"),
        )

