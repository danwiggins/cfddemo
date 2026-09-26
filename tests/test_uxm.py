"""Hand-checkable tests for bounded fragment-level UXM classification."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from evidence_inspector.cell_origin_models import (
    CpgCallState,
    GenomicMarker,
    UxmState,
)
from evidence_inspector.uxm import (
    UxmStopReason,
    classify_uxm_calls,
)


def marker(
    marker_id: str = "marker.one",
    *,
    start0: int = 100,
    end0: int = 200,
) -> GenomicMarker:
    return GenomicMarker(
        marker_id=marker_id,
        chromosome="chr1",
        start0=start0,
        end0=end0,
        target_cell_type_id="cell.one",
        atlas_id="atlas.test",
        source_ids=("source.test",),
    )


def call(
    digest_character: str,
    position0: int,
    state: CpgCallState | str,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fragment_digest": digest_character * 64,
        "chromosome": "chr1",
        "position0": position0,
        "strand": "+",
        "canonical_base": "C",
        "modification_code": "m",
        "modified_probability": 0.99,
        "state": state,
        "passed": True,
    }
    payload.update(overrides)
    return payload


def fragment_calls(
    digest_character: str,
    states: list[CpgCallState],
    *,
    start0: int = 110,
) -> list[dict[str, Any]]:
    return [
        call(digest_character, start0 + offset, state)
        for offset, state in enumerate(states)
    ]


def test_hand_countable_uxm_classes_and_h_code() -> None:
    calls = [
        *fragment_calls(
            "a",
            [
                CpgCallState.METHYLATED,
                CpgCallState.UNMETHYLATED,
                CpgCallState.UNMETHYLATED,
                CpgCallState.UNMETHYLATED,
            ],
        ),
        *fragment_calls(
            "b",
            [
                CpgCallState.METHYLATED,
                CpgCallState.METHYLATED,
                CpgCallState.UNMETHYLATED,
                CpgCallState.UNMETHYLATED,
            ],
            start0=120,
        ),
        *fragment_calls(
            "c",
            [
                CpgCallState.METHYLATED,
                CpgCallState.METHYLATED,
                CpgCallState.METHYLATED,
                CpgCallState.UNMETHYLATED,
            ],
            start0=130,
        ),
    ]
    calls[0]["modification_code"] = "h"

    result = classify_uxm_calls(calls, [marker()])

    assert [item.methylation_fraction for item in result.observations] == [
        0.25,
        0.5,
        0.75,
    ]
    assert [item.state for item in result.observations] == [
        UxmState.U,
        UxmState.X,
        UxmState.M,
    ]
    assert all(
        cpg.modification_code == "m"
        for observation in result.observations
        for cpg in observation.cpg_calls
    )
    assert result.marker_counts[0].model_dump() == {
        "marker_id": "marker.one",
        "u_count": 1,
        "x_count": 1,
        "m_count": 1,
        "classified_fragment_count": 3,
        "u_fraction": 1 / 3,
    }


def test_minimum_rlen_is_four_after_defensive_filtering() -> None:
    calls = fragment_calls(
        "a",
        [
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
        ],
    )
    calls.extend(
        [
            call("a", 113, CpgCallState.UNMETHYLATED, passed=False),
            call("a", 114, CpgCallState.UNMETHYLATED, canonical_base="A"),
            call("a", 115, CpgCallState.UNMETHYLATED, modification_code="a"),
        ]
    )

    result = classify_uxm_calls(calls, [marker()])

    assert result.observations == ()
    assert result.marker_counts == ()
    assert result.diagnostics.excluded_fewer_than_four_cpgs == 1
    assert result.diagnostics.excluded_failed_calls == 1
    assert result.diagnostics.excluded_non_c_calls == 1
    assert result.diagnostics.excluded_unsupported_modification_calls == 1


def test_duplicate_cpg_positions_are_counted_once_even_across_strands() -> None:
    calls = fragment_calls(
        "a",
        [
            CpgCallState.METHYLATED,
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
        ],
    )
    calls.append(
        call(
            "a",
            110,
            CpgCallState.METHYLATED,
            strand="-",
            modification_code="h",
        )
    )

    result = classify_uxm_calls(calls, [marker()])

    observation = result.observations[0]
    assert observation.callable_cpg_count == 4
    assert observation.methylated_cpg_count == 1
    assert observation.state == UxmState.U
    assert result.diagnostics.duplicate_cpg_calls == 1


def test_conflicting_duplicate_invalidates_the_fragment_marker_group() -> None:
    calls = fragment_calls(
        "a",
        [
            CpgCallState.METHYLATED,
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
            CpgCallState.UNMETHYLATED,
        ],
    )
    calls.append(call("a", 110, CpgCallState.UNMETHYLATED))

    result = classify_uxm_calls(calls, [marker()])

    assert result.observations == ()
    assert result.diagnostics.duplicate_cpg_calls == 1
    assert result.diagnostics.excluded_conflicting_duplicate_groups == 1


def test_overlapping_or_unknown_marker_assignment_fails_closed() -> None:
    overlapping = marker("marker.two", start0=105, end0=180)
    ambiguous = fragment_calls(
        "a",
        [CpgCallState.UNMETHYLATED] * 4,
    )
    ambiguous_result = classify_uxm_calls(
        ambiguous,
        [marker(), overlapping],
    )
    assert ambiguous_result.observations == ()
    assert ambiguous_result.diagnostics.excluded_ambiguous_marker_calls == 1

    unknown = fragment_calls(
        "b",
        [CpgCallState.UNMETHYLATED] * 4,
    )
    unknown[1]["marker_id"] = "marker.unknown"
    unknown_result = classify_uxm_calls(unknown, [marker()])
    assert unknown_result.observations == ()
    assert unknown_result.diagnostics.excluded_unknown_marker_calls == 1


def test_marker_counts_and_weights_use_classified_fragment_coverage() -> None:
    first = marker()
    second = marker("marker.two", start0=300, end0=400)
    calls = [
        *fragment_calls("a", [CpgCallState.UNMETHYLATED] * 4),
        *fragment_calls(
            "b",
            [CpgCallState.METHYLATED] * 4,
            start0=120,
        ),
        *fragment_calls(
            "c",
            [CpgCallState.UNMETHYLATED] * 4,
            start0=310,
        ),
    ]

    result = classify_uxm_calls(calls, [first, second])

    assert [row.marker_id for row in result.marker_counts] == [
        "marker.one",
        "marker.two",
    ]
    assert [weight.classified_fragment_count for weight in result.marker_weights] == [
        2,
        1,
    ]
    assert [weight.normalized_weight for weight in result.marker_weights] == [
        pytest.approx(2 / 3),
        pytest.approx(1 / 3),
    ]
    assert sum(weight.normalized_weight for weight in result.marker_weights) == (
        pytest.approx(1.0)
    )


def test_call_and_group_caps_bound_single_pass_consumption() -> None:
    consumed = 0

    def stream() -> Any:
        nonlocal consumed
        for position0 in range(110, 120):
            consumed += 1
            yield call("a", position0, CpgCallState.UNMETHYLATED)

    capped = classify_uxm_calls(stream(), [marker()], maximum_calls=4)
    assert consumed == 4
    assert capped.diagnostics.stop_reason == UxmStopReason.CALL_CAP
    assert capped.diagnostics.partial_input
    assert len(capped.observations) == 1

    grouped = classify_uxm_calls(
        [
            *fragment_calls("a", [CpgCallState.UNMETHYLATED] * 4),
            call("b", 120, CpgCallState.UNMETHYLATED),
        ],
        [marker()],
        maximum_groups=1,
    )
    assert grouped.diagnostics.stop_reason == UxmStopReason.GROUP_CAP
    assert grouped.diagnostics.partial_input
    assert len(grouped.observations) == 1


def test_oversized_group_is_excluded_instead_of_prefix_classified() -> None:
    calls = fragment_calls(
        "a",
        [CpgCallState.UNMETHYLATED] * 5,
    )

    result = classify_uxm_calls(
        calls,
        [marker()],
        maximum_cpgs_per_group=4,
    )

    assert result.observations == ()
    assert result.diagnostics.excluded_oversized_groups == 1


def test_diagnostics_are_aggregate_only_and_invalid_limits_fail() -> None:
    result = classify_uxm_calls(
        fragment_calls("a", [CpgCallState.UNMETHYLATED] * 4),
        [marker()],
    )
    serialized = repr(asdict(result.diagnostics))
    assert "a" * 64 not in serialized
    assert "read_id" not in serialized
    assert "path" not in serialized

    with pytest.raises(ValueError, match="maximum_calls"):
        classify_uxm_calls([], [marker()], maximum_calls=0)
    with pytest.raises(ValueError, match="at least one marker"):
        classify_uxm_calls([], [])
