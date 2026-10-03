"""D10 covariate context, confounding, privacy, and bounded-ingress tests."""

from __future__ import annotations

from functools import lru_cache

import pytest

import evidence_inspector.covariate_context as covariate_module
from evidence_inspector.covariate_context import (
    ALL_COVARIATE_DIMENSIONS,
    MAX_COVARIATE_MEMBERS,
    AggregateCovariateSummary,
    CovariateClassification,
    CovariateDimension,
    CovariateReason,
    CovariateValue,
    CovariateValueState,
    D03Role,
    D09PopulationDigestInput,
    D10CovariateInput,
    MemberCovariateContext,
    aggregate_covariate_summary_bytes,
    aggregate_covariate_summary_sha256,
    build_covariate_context,
    covariate_context_result_sha256,
    d10_covariate_input_sha256,
    project_aggregate_covariate_summary,
)
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalMemberDecision,
    LongitudinalOutcome,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
)
from tests.test_longitudinal_compatibility import _decide, _policy, _record

D02_POLICY_SHA256 = longitudinal_anchor_policy_sha256(_policy(_record("1")))


def _sha(digit: str) -> str:
    return digit * 64


def _token(digit: str) -> str:
    return f"covariate_{digit * 32}"


def _value(
    dimension: CovariateDimension,
    digit: str | None,
) -> CovariateValue:
    namespace = {
        CovariateDimension.BATCH: "a",
        CovariateDimension.PROTOCOL: "b",
        CovariateDimension.PREANALYTICS: "c",
    }[dimension]
    return CovariateValue(
        dimension=dimension,
        state=(
            CovariateValueState.UNKNOWN if digit is None else CovariateValueState.KNOWN
        ),
        token=None if digit is None else f"covariate_{namespace}{digit * 31}",
    )


@lru_cache(maxsize=len(LongitudinalOutcome))
def _d03_template(outcome: LongitudinalOutcome) -> LongitudinalMemberDecision:
    anchor = _record("1")
    if outcome is LongitudinalOutcome.EQUIVALENT:
        member = _record("2")
    elif outcome is LongitudinalOutcome.INCOMPATIBLE:
        member = _record("2", changed=ComparisonDimension.ASSAY_PROTOCOL)
    else:
        raise AssertionError(f"unsupported D10 test outcome: {outcome}")
    return _decide(anchor, member, _policy(anchor))


def _d03_decision(
    member_sha256: str,
    outcome: LongitudinalOutcome,
) -> LongitudinalMemberDecision:
    return _d03_template(outcome).model_copy(
        update={
            "member_result_id": f"result_{member_sha256[:16]}",
            "member_result_sha256": member_sha256,
        }
    )


def _member(
    digit: str,
    *,
    timepoint: str = "a",
    batch: str | None = "1",
    protocol: str | None = "2",
    preanalytics: str | None = "3",
    outcome: LongitudinalOutcome = LongitudinalOutcome.EQUIVALENT,
) -> MemberCovariateContext:
    member_sha256 = _sha(digit)
    decision = _d03_decision(member_sha256, outcome)
    return MemberCovariateContext(
        member_sha256=member_sha256,
        biological_timepoint_sha256=_sha(timepoint),
        d03_decision_sha256=longitudinal_member_decision_sha256(decision),
        d03_outcome=outcome,
        values=(
            _value(CovariateDimension.BATCH, batch),
            _value(CovariateDimension.PROTOCOL, protocol),
            _value(CovariateDimension.PREANALYTICS, preanalytics),
        ),
    )


def _input(*members: MemberCovariateContext) -> D10CovariateInput:
    included = tuple(sorted(member.member_sha256 for member in members))
    return D10CovariateInput(
        population=D09PopulationDigestInput(
            cohort_manifest_sha256=_sha("a"),
            d09_status_sha256=_sha("b"),
            d09_population_sha256=_sha("c"),
            d02_anchor_policy_sha256=D02_POLICY_SHA256,
            included_member_sha256s=included,
        ),
        members=tuple(members),
    )


def _build(
    value: D10CovariateInput,
    decisions: tuple[LongitudinalMemberDecision, ...] | None = None,
):
    if decisions is None:
        decisions = tuple(
            _d03_decision(member.member_sha256, member.d03_outcome)
            for member in value.members
        )
    return build_covariate_context(
        value,
        expected_d09_status_sha256=_sha("b"),
        expected_d09_population_sha256=_sha("c"),
        expected_d02_anchor_policy_sha256=D02_POLICY_SHA256,
        d03_member_decisions=decisions,
    )


def test_constant_complete_context_is_clear_and_descriptive_only() -> None:
    source = _input(_member("1"), _member("2", timepoint="b"))
    result = _build(source)

    assert result.classification is CovariateClassification.CLEAR
    assert result.reason_codes == (CovariateReason.COMPLETE_CONSTANT_CONTEXT,)
    assert len(result.groups) == 1
    assert result.groups[0].member_sha256s == source.population.included_member_sha256s
    assert not result.measurement_values_changed
    assert not result.comparison_eligibility_changed
    assert not result.silent_correction_applied
    assert not result.biological_attribution_allowed
    assert not result.clinical_interpretation_allowed
    assert not result.live_d09_registry_verified
    assert covariate_context_result_sha256(result)
    assert all(
        "value" not in name
        for name in result.__class__.model_fields
        if name != "measurement_values_changed"
    )


def test_protocol_perfectly_aligned_with_timepoint_is_aliased() -> None:
    result = _build(
        _input(
            _member("1", timepoint="a", protocol="1"),
            _member("2", timepoint="b", protocol="2"),
            _member("3", timepoint="b", protocol="2"),
        )
    )

    assert result.classification is CovariateClassification.ALIASED
    assert result.reason_codes == (CovariateReason.PROTOCOL_TIMEPOINT_ALIASED,)
    assert not result.biological_attribution_allowed


def test_missing_batch_is_unknown_not_a_synthetic_category() -> None:
    result = _build(_input(_member("1", batch=None), _member("2")))

    assert result.classification is CovariateClassification.MISSING_METADATA
    assert result.reason_codes == (CovariateReason.BATCH_METADATA_MISSING,)
    missing = result.groups[0].values[0]
    assert missing.state is CovariateValueState.UNKNOWN
    assert missing.token is None


def test_complete_nonaliased_variation_is_mixed() -> None:
    result = _build(
        _input(
            _member("1", timepoint="a", batch="1"),
            _member("2", timepoint="a", batch="2"),
            _member("3", timepoint="b", batch="1"),
        )
    )

    assert result.classification is CovariateClassification.MIXED
    assert result.reason_codes == (CovariateReason.COVARIATE_VARIATION_PRESENT,)


def test_protocol_change_at_one_timepoint_is_mixed_without_attribution() -> None:
    result = _build(
        _input(
            _member("1", timepoint="a", protocol="1"),
            _member("2", timepoint="a", protocol="2"),
        )
    )

    assert result.classification is CovariateClassification.MIXED
    assert result.reason_codes == (CovariateReason.COVARIATE_VARIATION_PRESENT,)
    assert not result.biological_attribution_allowed


def test_empty_population_is_explicit_missing_metadata() -> None:
    result = _build(_input())

    assert result.classification is CovariateClassification.MISSING_METADATA
    assert result.reason_codes == (CovariateReason.NO_INCLUDED_MEMBERS,)
    assert result.groups == ()


def test_member_order_does_not_change_result_identity() -> None:
    first = _member("1", batch="1")
    second = _member("2", timepoint="b", batch="2")

    forward = _build(_input(first, second))
    reverse = _build(_input(second, first))

    assert forward == reverse
    assert covariate_context_result_sha256(forward) == covariate_context_result_sha256(
        reverse
    )
    assert d10_covariate_input_sha256(_input(first, second)) == (
        d10_covariate_input_sha256(_input(second, first))
    )


def test_every_and_only_included_member_requires_context() -> None:
    member = _member("1")
    population = D09PopulationDigestInput(
        cohort_manifest_sha256=_sha("a"),
        d09_status_sha256=_sha("b"),
        d09_population_sha256=_sha("c"),
        d02_anchor_policy_sha256=_sha("d"),
        included_member_sha256s=(_sha("1"), _sha("2")),
    )
    with pytest.raises(ValueError, match="every and only"):
        D10CovariateInput(population=population, members=(member,))


def test_one_opaque_token_cannot_conflict_across_dimensions() -> None:
    token = _token("1")
    member = _member("1").model_copy(
        update={
            "values": (
                CovariateValue(
                    dimension=CovariateDimension.BATCH,
                    state=CovariateValueState.KNOWN,
                    token=token,
                ),
                CovariateValue(
                    dimension=CovariateDimension.PROTOCOL,
                    state=CovariateValueState.KNOWN,
                    token=token,
                ),
                _value(CovariateDimension.PREANALYTICS, "3"),
            )
        }
    )
    with pytest.raises(ValueError, match="two dimensions"):
        _input(member)


@pytest.mark.parametrize(
    ("field", "expected", "match"),
    (
        ("expected_d09_status_sha256", _sha("e"), "D09 status"),
        ("expected_d09_population_sha256", _sha("e"), "D09 population"),
        ("expected_d02_anchor_policy_sha256", _sha("e"), "D02 anchor"),
    ),
)
def test_every_authority_pin_is_exact(field: str, expected: str, match: str) -> None:
    arguments = {
        "expected_d09_status_sha256": _sha("b"),
        "expected_d09_population_sha256": _sha("c"),
        "expected_d02_anchor_policy_sha256": D02_POLICY_SHA256,
        "d03_member_decisions": (
            _d03_decision(_member("1").member_sha256, LongitudinalOutcome.EQUIVALENT),
        ),
    }
    arguments[field] = expected
    with pytest.raises(ValueError, match=match):
        build_covariate_context(_input(_member("1")), **arguments)


def test_d02_mismatch_is_bound_but_never_collapsed() -> None:
    member = _member("1")
    equivalent = _build(_input(member))
    relabeled_member = member.model_copy(
        update={"d03_outcome": LongitudinalOutcome.INCOMPATIBLE}
    )
    relabeled = _input(relabeled_member)
    forged_decision = _d03_decision(
        member.member_sha256, LongitudinalOutcome.EQUIVALENT
    ).model_copy(update={"outcome": LongitudinalOutcome.INCOMPATIBLE})

    with pytest.raises(TypeError, match="exact canonical artifact"):
        _build(relabeled, (forged_decision,))
    with pytest.raises(ValueError, match="does not match covariate input"):
        _build(relabeled)

    incompatible_member = _member("1", outcome=LongitudinalOutcome.INCOMPATIBLE)
    incompatible = _build(_input(incompatible_member))

    assert equivalent.groups == incompatible.groups
    assert equivalent.input_sha256 != incompatible.input_sha256
    assert equivalent.member_context_sha256s != incompatible.member_context_sha256s
    assert not incompatible.comparison_eligibility_changed


def test_forged_classification_or_reason_cannot_replay() -> None:
    clear = _build(_input(_member("1")))
    forged = clear.model_copy(
        update={
            "classification": CovariateClassification.ALIASED,
            "reason_codes": (CovariateReason.COMPLETE_CONSTANT_CONTEXT,),
        }
    )

    with pytest.raises(ValueError, match="classification"):
        covariate_context_result_sha256(forged)


@pytest.mark.parametrize(
    "field",
    (
        "input_sha256",
        "cohort_manifest_sha256",
        "d09_status_sha256",
        "d09_population_sha256",
        "d02_anchor_policy_sha256",
    ),
)
def test_result_recomputes_every_copied_input_identity(field: str) -> None:
    result = _build(_input(_member("1")))
    forged = result.model_copy(update={field: _sha("f")})

    with pytest.raises(ValueError, match="input identity"):
        covariate_context_result_sha256(forged)


def test_aggregate_projection_omits_protected_rows_timepoints_and_tokens() -> None:
    source = _input(
        _member("1", timepoint="a", batch="1"),
        _member("2", timepoint="b", batch="2"),
    )
    protected = _build(source)
    summary = project_aggregate_covariate_summary(protected)
    content = aggregate_covariate_summary_bytes(summary, protected_result=protected)

    assert isinstance(summary, AggregateCovariateSummary)
    assert summary.protected_context_sha256 == covariate_context_result_sha256(
        protected
    )
    assert summary.included_member_count == 2
    assert sum(group.member_count for group in summary.groups) == 2
    assert aggregate_covariate_summary_sha256(summary, protected_result=protected)
    protected_values = (
        *protected.included_member_sha256s,
        *(member.biological_timepoint_sha256 for member in protected.member_contexts),
        *(
            value.token
            for member in protected.member_contexts
            for value in member.values
            if value.token is not None
        ),
        protected.d09_status_sha256,
        protected.d09_population_sha256,
    )
    assert all(value.encode("ascii") not in content for value in protected_values)


def test_aggregate_group_order_does_not_depend_on_protected_tokens() -> None:
    exported = set()
    for singleton_batch in "123456789":
        protected = _build(
            _input(
                _member("1", batch=singleton_batch),
                _member("2", batch="0"),
                _member("3", batch="0"),
            )
        )
        summary = project_aggregate_covariate_summary(protected)
        assert aggregate_covariate_summary_bytes(summary, protected_result=protected)
        exported.add(
            tuple((group.states, group.member_count) for group in summary.groups)
        )

    assert len(exported) == 1


def test_d03_authority_is_never_claimed_as_verified() -> None:
    protected = _build(_input(_member("1")))
    summary = project_aggregate_covariate_summary(protected)

    assert protected.d03_authority_verified is False
    assert summary.d03_authority_verified is False
    for model, value in (
        (type(protected), protected),
        (type(summary), summary),
    ):
        with pytest.raises(ValueError):
            model.model_validate(
                {**value.model_dump(mode="python"), "d03_authority_verified": True}
            )


def test_aggregate_summary_rejects_forged_classification_and_reasons() -> None:
    protected = _build(
        _input(
            _member("1", timepoint="a", batch="1"),
            _member("2", timepoint="a", batch="2"),
        )
    )
    summary = project_aggregate_covariate_summary(protected)
    assert summary.classification is CovariateClassification.MIXED
    forged = summary.model_copy(
        update={
            "classification": CovariateClassification.ALIASED,
            "reason_codes": (CovariateReason.PROTOCOL_TIMEPOINT_ALIASED,),
        }
    )

    with pytest.raises(ValueError, match="protected context"):
        aggregate_covariate_summary_sha256(
            forged,
            protected_result=protected,
        )


def test_d03_decisions_require_exact_coverage_and_builtin_tuple() -> None:
    source = _input(_member("1"))
    source_decision = _d03_decision(
        source.members[0].member_sha256, LongitudinalOutcome.EQUIVALENT
    )

    with pytest.raises(ValueError, match="exact population"):
        _build(source, ())
    with pytest.raises(ValueError, match="exact population"):
        _build(
            source,
            (
                source_decision,
                _d03_decision(_sha("f"), LongitudinalOutcome.EQUIVALENT),
            ),
        )
    with pytest.raises(TypeError, match="exact tuple"):
        build_covariate_context(
            source,
            expected_d09_status_sha256=_sha("b"),
            expected_d09_population_sha256=_sha("c"),
            expected_d02_anchor_policy_sha256=D02_POLICY_SHA256,
            d03_member_decisions=[source_decision],  # type: ignore[arg-type]
        )


def test_d03_decision_policy_must_match_d02_anchor_policy() -> None:
    member = _member("1")
    wrong_policy = _d03_decision(
        member.member_sha256, LongitudinalOutcome.EQUIVALENT
    ).model_copy(update={"policy_sha256": _sha("e")})
    rebound = member.model_copy(
        update={
            "d03_decision_sha256": covariate_module._d03_decision_sha256(wrong_policy)
        }
    )

    with pytest.raises(ValueError, match="D02 anchor policy"):
        _build(_input(rebound), (wrong_policy,))


@pytest.mark.parametrize("attribute", ("__pydantic_private__", "__pydantic_extra__"))
def test_d03_hidden_state_rejects_before_serialization(attribute: str) -> None:
    member = _member("1")
    decision = _d03_decision(member.member_sha256, member.d03_outcome).model_copy()
    object.__setattr__(decision, attribute, {"private": "/tmp/forged"})

    with pytest.raises(TypeError, match="exact canonical artifact"):
        _build(_input(member), (decision,))


def test_d03_extra_state_and_oversized_collection_reject_before_serialization() -> None:
    member = _member("1")
    decision = _d03_decision(member.member_sha256, member.d03_outcome)
    extra = decision.model_copy()
    object.__getattribute__(extra, "__dict__")["private_path"] = "/tmp/forged"
    oversized = decision.model_copy(
        update={"dimension_explanations": decision.dimension_explanations * 1_001}
    )

    for forged in (extra, oversized):
        with pytest.raises(TypeError, match="exact canonical artifact"):
            _build(_input(member), (forged,))


def test_d03_decisions_share_one_collection_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = len(
        covariate_module._d03_decision_bytes(
            _d03_decision(_sha("1"), LongitudinalOutcome.EQUIVALENT)
        )
    )
    assert (
        one * MAX_COVARIATE_MEMBERS < covariate_module.MAX_D03_DECISIONS_TOTAL_BYTES
    )
    monkeypatch.setattr(covariate_module, "MAX_D03_DECISIONS_TOTAL_BYTES", one + 1)

    with pytest.raises(TypeError, match="collection byte budget"):
        _build(_input(_member("1"), _member("2")))


def test_d03_decision_authority_is_unique_and_member_specific() -> None:
    first = _member("1")
    second = _member("2").model_copy(
        update={"d03_decision_sha256": first.d03_decision_sha256}
    )
    with pytest.raises(ValueError, match="unique D03 decision"):
        _input(first, second)

    distinct = _input(_member("1"), _member("2"))
    first, second = distinct.members
    first_decision = _d03_decision(first.member_sha256, first.d03_outcome)
    second_decision = _d03_decision(second.member_sha256, second.d03_outcome)
    with pytest.raises(ValueError, match="unique members"):
        _build(distinct, (first_decision, first_decision))
    swapped = (
        first_decision.model_copy(
            update={"member_result_sha256": second.member_sha256}
        ),
        second_decision.model_copy(
            update={"member_result_sha256": first.member_sha256}
        ),
    )
    with pytest.raises(ValueError, match="does not match covariate input"):
        _build(distinct, swapped)


def test_private_or_oversized_forged_graph_rejects_before_serialization() -> None:
    source = _input(_member("1"))
    oversized = source.model_copy(
        update={"members": source.members * (MAX_COVARIATE_MEMBERS + 1)}
    )
    serializer_calls = 0

    class UnexpectedSerializer:
        def to_python(self, *args: object, **kwargs: object) -> object:
            nonlocal serializer_calls
            del args, kwargs
            serializer_calls += 1
            raise AssertionError("forged graph reached serialization")

    with pytest.raises(TypeError, match="object graph"):
        covariate_module._exact_bytes(
            oversized, D10CovariateInput, UnexpectedSerializer()
        )

    poisoned = source.model_copy()
    object.__getattribute__(poisoned, "__dict__")["private_path"] = "/tmp/private"
    with pytest.raises(TypeError, match="object graph"):
        covariate_module._exact_bytes(
            poisoned, D10CovariateInput, UnexpectedSerializer()
        )

    class PoisonKey:
        hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            return 1

    hostile_state = source.model_copy()
    poison_key = PoisonKey()
    object.__getattribute__(hostile_state, "__dict__")[poison_key] = None
    poison_key.hash_calls = 0
    with pytest.raises(TypeError, match="object graph"):
        covariate_module._exact_bytes(
            hostile_state, D10CovariateInput, UnexpectedSerializer()
        )
    assert serializer_calls == 0
    assert poison_key.hash_calls == 0


@pytest.mark.parametrize("attribute", ("__pydantic_private__", "__pydantic_extra__"))
@pytest.mark.parametrize("nested", (False, True))
def test_hidden_pydantic_state_rejects_at_every_model_depth(
    attribute: str,
    nested: bool,
) -> None:
    source = _input(_member("1"))
    if nested:
        value = source.members[0].values[0].model_copy()
        object.__setattr__(value, attribute, {"private": "forged"})
        member = source.members[0].model_copy(
            update={"values": (value, *source.members[0].values[1:])}
        )
        forged = source.model_copy(update={"members": (member,)})
    else:
        forged = source.model_copy()
        object.__setattr__(forged, attribute, {"private": "forged"})

    with pytest.raises(TypeError, match="object graph"):
        _build(forged)


def test_pathlike_and_identity_bearing_tokens_are_not_valid_covariates() -> None:
    for token in ("/tmp/batch", "patient_00000000000000000000000000000000"):
        with pytest.raises(ValueError):
            CovariateValue(
                dimension=CovariateDimension.BATCH,
                state=CovariateValueState.KNOWN,
                token=token,
            )


def test_dimension_order_is_exact() -> None:
    member = _member("1")
    with pytest.raises(ValueError, match="every dimension in order"):
        MemberCovariateContext(
            member_sha256=member.member_sha256,
            biological_timepoint_sha256=member.biological_timepoint_sha256,
            d03_decision_sha256=member.d03_decision_sha256,
            d03_outcome=member.d03_outcome,
            values=tuple(reversed(member.values)),
        )


def test_covariate_dimension_contract_is_complete() -> None:
    assert ALL_COVARIATE_DIMENSIONS == (
        CovariateDimension.BATCH,
        CovariateDimension.PROTOCOL,
        CovariateDimension.PREANALYTICS,
    )


def test_maximum_member_population_remains_bounded_and_valid() -> None:
    decisions = tuple(
        _d03_decision(f"{index:064x}", LongitudinalOutcome.EQUIVALENT)
        for index in range(MAX_COVARIATE_MEMBERS)
    )
    members = tuple(
        MemberCovariateContext(
            member_sha256=f"{index:064x}",
            biological_timepoint_sha256=_sha("a"),
            d03_decision_sha256=longitudinal_member_decision_sha256(decisions[index]),
            d03_outcome=LongitudinalOutcome.EQUIVALENT,
            values=(
                CovariateValue(
                    dimension=CovariateDimension.BATCH,
                    state=CovariateValueState.KNOWN,
                    token=f"covariate_{index:032x}",
                ),
                CovariateValue(
                    dimension=CovariateDimension.PROTOCOL,
                    state=CovariateValueState.KNOWN,
                    token=f"covariate_{index + 2 * MAX_COVARIATE_MEMBERS:032x}",
                ),
                CovariateValue(
                    dimension=CovariateDimension.PREANALYTICS,
                    state=CovariateValueState.KNOWN,
                    token=f"covariate_{index + 4 * MAX_COVARIATE_MEMBERS:032x}",
                ),
            ),
        )
        for index in range(MAX_COVARIATE_MEMBERS)
    )

    result = _build(_input(*members), decisions)
    summary = project_aggregate_covariate_summary(result)
    aggregate_bytes = aggregate_covariate_summary_bytes(
        summary, protected_result=result
    )

    assert len(result.included_member_sha256s) == MAX_COVARIATE_MEMBERS
    assert len(result.member_context_sha256s) == MAX_COVARIATE_MEMBERS
    assert len(result.groups) == MAX_COVARIATE_MEMBERS
    assert result.classification is CovariateClassification.MIXED
    assert len(summary.groups) == MAX_COVARIATE_MEMBERS
    assert aggregate_bytes

    at_node_limit = result.model_copy(
        update={
            "groups": tuple(
                group.model_copy(update={"member_sha256s": group.member_sha256s * 16})
                for group in result.groups
            )
        }
    )
    assert covariate_module._exact_bytes(
        at_node_limit,
        covariate_module.CovariateContextResult,
        covariate_module._CODECS[covariate_module.CovariateContextResult][0],
    )
    forged = at_node_limit.model_copy(
        update={
            "groups": tuple(
                group.model_copy(
                    update={
                        "member_sha256s": (
                            *group.member_sha256s,
                            group.member_sha256s[0],
                        )
                    }
                )
                for group in at_node_limit.groups
            )
        }
    )
    serializer_calls = 0

    class UnexpectedSerializer:
        def to_python(self, *args: object, **kwargs: object) -> object:
            nonlocal serializer_calls
            del args, kwargs
            serializer_calls += 1
            raise AssertionError("over-budget graph reached serialization")

    with pytest.raises(TypeError, match="object graph"):
        covariate_module._exact_bytes(
            forged,
            covariate_module.CovariateContextResult,
            UnexpectedSerializer(),
        )
    assert serializer_calls == 0


def _anchor_row(member_sha256: str, *, batch: str = "1") -> MemberCovariateContext:
    return MemberCovariateContext(
        member_sha256=member_sha256,
        biological_timepoint_sha256=_sha("0"),
        d03_role=D03Role.ANCHOR,
        d03_decision_sha256=None,
        d03_outcome=None,
        values=(
            _value(CovariateDimension.BATCH, batch),
            _value(CovariateDimension.PROTOCOL, "2"),
            _value(CovariateDimension.PREANALYTICS, "3"),
        ),
    )


def _build_anchored(value, decisions, anchor_sha256):
    return covariate_module._build_covariate_context(
        value,
        expected_d09_status_sha256=_sha("b"),
        expected_d09_population_sha256=_sha("c"),
        expected_d02_anchor_policy_sha256=D02_POLICY_SHA256,
        d03_member_decisions=decisions,
        d03_anchor_result_sha256=anchor_sha256,
    )


def test_anchor_row_carries_no_d03_decision_and_members_require_one() -> None:
    member = _member("1")
    fields = member.model_dump()
    with pytest.raises(ValueError, match="anchor row cannot carry"):
        MemberCovariateContext.model_validate({**fields, "d03_role": "anchor"})
    for update in ({"d03_decision_sha256": None}, {"d03_outcome": None}):
        with pytest.raises(ValueError, match="requires its D03 decision"):
            MemberCovariateContext.model_validate({**fields, **update})
    with pytest.raises(ValueError, match="at most one D03 anchor"):
        _input(_anchor_row(_sha("9")), _anchor_row(_sha("8")))
    assert member.d03_role is D03Role.MEMBER


def test_only_the_pinned_series_anchor_is_admitted_without_a_decision() -> None:
    member = _member("5")
    decision = _d03_decision(member.member_sha256, member.d03_outcome)
    anchor_sha256 = decision.anchor_result_sha256
    source = _input(_anchor_row(anchor_sha256, batch="4"), member)

    result = _build_anchored(source, (decision,), anchor_sha256)
    rows = {item.member_sha256: item for item in result.member_contexts}
    assert rows[anchor_sha256].d03_role is D03Role.ANCHOR
    assert rows[anchor_sha256].d03_decision_sha256 is None
    assert result.included_member_sha256s == tuple(sorted(rows))
    # The anchor's covariates take part in grouping and classification.
    assert result.classification is CovariateClassification.MIXED
    assert len(result.groups) == 2
    assert result.schema_version == "traceback.covariate-context-result.v2"

    # The caller-supplied path admits no anchor row at all.
    with pytest.raises(ValueError, match="does not match the D03 series anchor"):
        _build(source, (decision,))
    # The anchor row must be the pinned series anchor ...
    with pytest.raises(ValueError, match="does not match the D03 series anchor"):
        _build_anchored(source, (decision,), _sha("e"))
    # ... even when the pin and every decision agree on another anchor ...
    elsewhere = decision.model_copy(update={"anchor_result_sha256": _sha("e")})
    with pytest.raises(ValueError, match="does not match the D03 series anchor"):
        _build_anchored(source, (elsewhere,), _sha("e"))
    # ... which every decision names ...
    with pytest.raises(ValueError, match="does not match the D03 series anchor"):
        _build_anchored(
            source,
            (decision.model_copy(update={"anchor_result_sha256": _sha("e")}),),
            anchor_sha256,
        )
    # ... and none decides.
    reflexive = _d03_decision(anchor_sha256, LongitudinalOutcome.EQUIVALENT)
    with pytest.raises(ValueError, match="does not match the D03 series anchor"):
        _build_anchored(source, (decision, reflexive), anchor_sha256)
    # A decided member still needs its decision; the anchor pin cannot cover it.
    with pytest.raises(ValueError, match="do not cover the exact population"):
        _build_anchored(source, (), anchor_sha256)


def test_v1_d10_contracts_fail_closed() -> None:
    result = _build(_input(_member("1")))
    encoded = covariate_module._exact_bytes(
        result,
        covariate_module.CovariateContextResult,
        covariate_module._CODECS[covariate_module.CovariateContextResult][0],
    )
    assert b'"traceback.covariate-context-result.v2"' in encoded
    legacy = encoded.replace(
        b"traceback.covariate-context-result.v2",
        b"traceback.covariate-context-result.v1",
    )
    with pytest.raises(ValueError):
        covariate_module.CovariateContextResult.model_validate_json(legacy)
    source = _input(_member("1"))
    assert source.schema_version == "traceback.d10-covariate-input.v2"
    with pytest.raises(ValueError):
        D10CovariateInput.model_validate(
            {
                **source.model_dump(),
                "schema_version": "traceback.d10-covariate-input.v1",
            }
        )
