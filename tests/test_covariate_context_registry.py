"""D10 covariate context wired to the protected D03 decision registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from evidence_inspector.covariate_context import (
    CovariateDimension,
    D09PopulationDigestInput,
    D10CovariateInput,
    MemberCovariateContext,
    RegisteredCovariateContext,
    build_covariate_context,
    build_registered_covariate_context,
    registered_covariate_context_bytes,
    verify_registered_covariate_context,
)
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalOutcome,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    LongitudinalDecisionRegistryConflict,
    LongitudinalDecisionRegistryStale,
)
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
)
from tests.test_covariate_context import _value
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    NOW,
    PROVIDER,
    TRUST_SHA256,
    _policy,
    _record,
)

PINS = {PROVIDER: TRUST_SHA256}


@pytest.fixture
def live(tmp_path: Path):
    store = ProviderLinkageStore(
        tmp_path / "linkage",
        expected_trust_snapshot_sha256_by_provider=PINS,
        time_source=AuthorityTimeSource.fixed(NOW),
    )
    registry = None
    try:
        records = (
            _record("1"),
            _record("2"),
            _record("3", changed=ComparisonDimension.ASSAY_PROTOCOL),
        )
        for record in records:
            assert record.authorized_linkage is not None
            store.commit_authorized_revision(record.authorized_linkage)
        receipts = {item.linkage_id: item for item in store.active_snapshot().receipts}
        active = tuple(
            record.model_copy(
                update={
                    "activation_receipt": receipts[record.linkage_revision.linkage_id]
                }
            )
            for record in records
        )
        registry = LongitudinalDecisionRegistry(
            tmp_path / "d03",
            linkage_store=store,
            expected_trust_snapshot_sha256_by_provider=PINS,
        )
        policy = _policy(active[0])
        receipt = registry.register_series(
            active[0],
            active[1:],
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
        )
        yield store, registry, receipt, active
    finally:
        if registry is not None:
            registry.close()
        store.close()


def _input(live, *, members=None, digest_override=None) -> D10CovariateInput:
    _, registry, receipt, _ = live
    series = registry.resolve(receipt.selector_id).decision
    decisions = series.decisions if members is None else members(series.decisions)
    contexts = tuple(
        MemberCovariateContext(
            member_sha256=decision.member_result_sha256,
            biological_timepoint_sha256=str(index) * 64,
            d03_decision_sha256=(
                digest_override or longitudinal_member_decision_sha256(decision)
            ),
            d03_outcome=decision.outcome,
            values=(
                _value(CovariateDimension.BATCH, "1"),
                _value(CovariateDimension.PROTOCOL, str(index)),
                _value(CovariateDimension.PREANALYTICS, "3"),
            ),
        )
        for index, decision in enumerate(decisions, start=1)
    )
    return D10CovariateInput(
        population=D09PopulationDigestInput(
            cohort_manifest_sha256="a" * 64,
            d09_status_sha256="b" * 64,
            d09_population_sha256="c" * 64,
            d02_anchor_policy_sha256=series.policy_sha256,
            included_member_sha256s=tuple(
                sorted(item.member_sha256 for item in contexts)
            ),
        ),
        members=contexts,
    )


def _build(live, value: D10CovariateInput, *, selector: str | None = None):
    _, registry, receipt, _ = live
    return build_registered_covariate_context(
        value,
        expected_d09_status_sha256="b" * 64,
        expected_d09_population_sha256="c" * 64,
        expected_d02_anchor_policy_sha256=value.population.d02_anchor_policy_sha256,
        decision_registry=registry,
        series_selector_id=selector or receipt.selector_id,
    )


def _advance(live) -> None:
    extra = _record("4")
    assert extra.authorized_linkage is not None
    live[0].commit_authorized_revision(extra.authorized_linkage)


def test_registered_context_uses_registry_decisions_and_binds_the_series(live) -> None:
    _, registry, receipt, _ = live
    value = _input(live)
    registered = _build(live, value)
    resolved = registry.resolve(receipt.selector_id)

    assert isinstance(registered, RegisteredCovariateContext)
    assert registered.d03_authority_verified is True
    assert registered.live_d09_registry_verified is False
    binding = registered.d03_series
    assert (binding.registry_id, binding.selector_id, binding.object_sha256) == (
        receipt.registry_id,
        receipt.selector_id,
        receipt.object_sha256,
    )
    assert binding.series_decision_sha256 == resolved.decision_sha256
    assert binding.linkage_snapshot_sha256 == resolved.decision.linkage_snapshot_sha256
    plain = build_covariate_context(
        value,
        expected_d09_status_sha256="b" * 64,
        expected_d09_population_sha256="c" * 64,
        expected_d02_anchor_policy_sha256=value.population.d02_anchor_policy_sha256,
        d03_member_decisions=resolved.decision.decisions,
    )
    assert registered.context == plain
    assert registered.context.d03_authority_verified is False
    outcomes = {
        member.d03_outcome for member in registered.context.member_contexts
    }
    assert outcomes == {LongitudinalOutcome.EQUIVALENT, LongitudinalOutcome.INCOMPATIBLE}
    assert registered_covariate_context_bytes(registered)


def test_population_may_be_a_subset_of_the_series_but_not_exceed_it(live) -> None:
    subset = _input(live, members=lambda decisions: decisions[:1])
    assert len(_build(live, subset).context.member_contexts) == 1

    outside = subset.model_copy(
        update={
            "population": subset.population.model_copy(
                update={"included_member_sha256s": ("f" * 64,)}
            ),
            "members": (
                subset.members[0].model_copy(update={"member_sha256": "f" * 64}),
            ),
        }
    )
    with pytest.raises(ValueError, match="does not cover the D10 population"):
        _build(live, outside)


def test_declared_decision_that_differs_from_the_registry_is_rejected(live) -> None:
    _, registry, receipt, _ = live
    decisions = registry.resolve(receipt.selector_id).decision.decisions
    swapped = longitudinal_member_decision_sha256(decisions[1])
    value = _input(live, members=lambda items: items[:1], digest_override=swapped)
    with pytest.raises(ValueError, match="does not match covariate input"):
        _build(live, value)


def test_policy_pin_and_registry_type_are_exact(live) -> None:
    value = _input(live)
    other_policy = value.model_copy(
        update={
            "population": value.population.model_copy(
                update={"d02_anchor_policy_sha256": "e" * 64}
            )
        }
    )
    with pytest.raises(ValueError, match="policy"):
        _build(live, other_policy)
    with pytest.raises(TypeError, match="exact D03 decision registry"):
        build_registered_covariate_context(
            value,
            expected_d09_status_sha256="b" * 64,
            expected_d09_population_sha256="c" * 64,
            expected_d02_anchor_policy_sha256=value.population.d02_anchor_policy_sha256,
            decision_registry=object(),  # type: ignore[arg-type]
            series_selector_id=live[2].selector_id,
        )
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="selector"):
        _build(live, value, selector="d03_series_" + "a" * 40)


def test_linkage_advance_blocks_build_and_verification(live) -> None:
    value = _input(live)
    registered = _build(live, value)
    _advance(live)

    with pytest.raises(LongitudinalDecisionRegistryStale):
        _build(live, value)
    with pytest.raises(LongitudinalDecisionRegistryStale):
        verify_registered_covariate_context(registered, decision_registry=live[1])


def test_verification_rebuilds_and_tolerates_unrelated_registry_growth(live) -> None:
    store, registry, receipt, active = live
    registered = _build(live, _input(live))
    policy = _policy(active[0])
    registry.register_series(
        active[0],
        active[1:2],
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
    )

    verified = verify_registered_covariate_context(registered, decision_registry=registry)

    assert verified.context == registered.context
    assert verified.d03_series.state_version == 2
    assert verified.d03_series.state_head_sha256 != (
        registered.d03_series.state_head_sha256
    )
    assert store is live[0] and receipt.selector_id == verified.d03_series.selector_id


def test_verification_rejects_a_forged_binding_or_context(live) -> None:
    registered = _build(live, _input(live))
    binding = registered.d03_series
    assert binding.linkage_snapshot_state_version is not None
    for update in (
        {"series_decision_sha256": "0" * 64},
        {"object_sha256": "0" * 64},
        {"registry_epoch_sha256": "0" * 64},
        {"linkage_snapshot_state_version": binding.linkage_snapshot_state_version + 1},
        {"linkage_snapshot_state_head_sha256": "0" * 64},
    ):
        forged = registered.model_copy(
            update={"d03_series": binding.model_copy(update=update)}
        )
        with pytest.raises(ValueError, match="not current"):
            verify_registered_covariate_context(forged, decision_registry=live[1])

    member = registered.context.member_contexts[0]
    relabelled_member = member.model_copy(
        update={"d03_outcome": LongitudinalOutcome.EQUIVALENT}
        if member.d03_outcome is not LongitudinalOutcome.EQUIVALENT
        else {"d03_outcome": LongitudinalOutcome.INCOMPATIBLE}
    )
    relabelled = registered.model_copy(
        update={
            "context": registered.context.model_copy(
                update={
                    "member_contexts": (
                        relabelled_member,
                        *registered.context.member_contexts[1:],
                    )
                }
            )
        }
    )
    with pytest.raises((ValueError, TypeError)):
        verify_registered_covariate_context(relabelled, decision_registry=live[1])
    with pytest.raises(TypeError, match="exact type"):
        verify_registered_covariate_context(
            registered.model_dump(), decision_registry=live[1]  # type: ignore[arg-type]
        )


def test_linkage_snapshot_binding_must_be_complete(live) -> None:
    registered = _build(live, _input(live))
    values = registered.d03_series.model_dump(mode="python")
    for field in (
        "linkage_snapshot_state_version",
        "linkage_snapshot_state_head_sha256",
        "linkage_snapshot_sha256",
    ):
        with pytest.raises(ValueError, match="must be complete"):
            type(registered.d03_series)(**{**values, field: None})
