"""Tests for the isolated experimental whole-chromosome dosage screen."""

import math

import pytest

from evidence_inspector.copy_number import (
    AUTOSOMES,
    CopyNumberResultBundle,
    compute_dosage,
)

DIGEST = "a" * 64


def _flat_counts(value: int = 100) -> dict[str, tuple[int, ...]]:
    return {chromosome: (value,) * 6 for chromosome in AUTOSOMES}


def test_flat_genome_stays_within_threshold() -> None:
    bundle = compute_dosage(
        _flat_counts(),
        input_artifact_sha256=DIGEST,
        accepted_read_count=13_200,
    )

    assert bundle.flagged_chromosome_count == 0
    assert all(row.classification == "within_threshold" for row in bundle.chromosomes)
    assert all(row.estimated_copy_number == 2.0 for row in bundle.chromosomes)


def test_broad_gain_is_flagged_and_low_bin_is_excluded() -> None:
    counts = _flat_counts()
    counts["chr8"] = (145, 145, 145, 145, 145, 1)

    bundle = compute_dosage(
        counts,
        input_artifact_sha256=DIGEST,
        accepted_read_count=13_200,
    )
    chromosome = bundle.chromosomes[7]

    assert chromosome.retained_bin_count == 5
    assert chromosome.classification == "gain_screen"
    assert chromosome.log2_ratio == pytest.approx(math.log2(1.45))
    assert chromosome.estimated_copy_number == pytest.approx(2.9)


def test_contract_rejects_changed_derived_copy_number() -> None:
    bundle = compute_dosage(
        _flat_counts(),
        input_artifact_sha256=DIGEST,
        accepted_read_count=13_200,
    )
    payload = bundle.model_dump(mode="python")
    payload["chromosomes"][0]["estimated_copy_number"] = 3.0

    with pytest.raises(ValueError, match="estimated copy number"):
        CopyNumberResultBundle.model_validate(payload)


def test_missing_chromosome_fails_closed() -> None:
    counts = _flat_counts()
    del counts["chr22"]

    with pytest.raises(ValueError, match="chr22"):
        compute_dosage(
            counts,
            input_artifact_sha256=DIGEST,
            accepted_read_count=13_200,
        )
