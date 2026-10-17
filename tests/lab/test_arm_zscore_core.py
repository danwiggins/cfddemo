"""Deterministic numerical and availability checks for the L2 arm core."""

import math

import pytest

from traceback_lab.arm_zscore_core import ArmObservation, score_arm

TARGET = "f" * 64
PROTOCOL = "a" * 64
ASSET = "b" * 64


def _controls(n: int = 10) -> list[ArmObservation]:
    return [ArmObservation(f"{index + 1:064x}", "chr1p", PROTOCOL, ASSET, float(index)) for index in range(n)]


def _score(observations: list[ArmObservation], **overrides):
    options = dict(
        target_source_id=TARGET, arm_id="chr1p", target_log2=9.5,
        target_protocol_key=PROTOCOL, eligible_reads=2_000_000,
        arm_asset_sha256=ASSET, observations=observations,
    )
    options.update(overrides)
    return score_arm(**options)


def test_arm_score_uses_only_matched_independent_values() -> None:
    controls = _controls() + [
        ArmObservation(TARGET, "chr1p", PROTOCOL, ASSET, 1_000.0),
        ArmObservation("c" * 64, "chr1p", "d" * 64, ASSET, 1_000.0),
        ArmObservation("e" * 64, "chr2q", PROTOCOL, ASSET, 1_000.0),
        ArmObservation("d" * 64, "chr1p", PROTOCOL, "c" * 64, 1_000.0),
    ]
    result = _score(controls)
    assert result.state == "ok" and result.reference_n == 10
    assert result.reference_mean == 4.5
    assert result.reference_sd == pytest.approx(math.sqrt(8.25))
    assert result.z == pytest.approx(5 / math.sqrt(8.25))


@pytest.mark.parametrize("change,state", [
    ({"arm_asset_sha256": None}, "missing_asset"),
    ({"eligible_reads": 1_999_999}, "insufficient_depth"),
    ({"eligible_reads": None}, "insufficient_depth"),
    ({"target_log2": None}, "missing_target"),
    ({"target_protocol_key": None}, "missing_protocol"),
])
def test_arm_score_exposes_unavailable_state(change, state) -> None:
    result = _score(_controls(), **change)
    assert result.state == state and result.z is None


def test_reference_gate_applies_after_source_exclusion() -> None:
    result = _score(_controls(9) + [ArmObservation(TARGET, "chr1p", PROTOCOL, ASSET, 9.0)])
    assert result.state == "no_reference" and result.reference_n == 9


def test_zero_variance_has_no_numeric_z_or_cutoff() -> None:
    controls = [ArmObservation(item.source_id, item.arm_id, item.protocol_key, item.arm_asset_sha256, 0.25)
                for item in _controls()]
    result = _score(controls)
    assert result.state == "zero_variance" and result.z is None and result.reference_sd == 0


def test_duplicate_source_or_nonfinite_value_is_refused() -> None:
    with pytest.raises(ValueError, match="distinct"):
        _score(_controls() + [_controls()[0]])
    with pytest.raises(ValueError, match="finite"):
        _score(_controls(), target_log2=float("nan"))
