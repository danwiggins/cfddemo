"""D10 covariate context derived from the live D09 registry population."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

import evidence_inspector.covariate_context as covariate_module
from evidence_inspector.cohort_import import CohortRecordCatalog
from evidence_inspector.cohort_manifest import (
    MeasurementAnchor,
    MemberLineageRole,
    build_cohort_member,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.covariate_context import (
    CovariateClassification,
    CovariateDimension,
    D03Role,
    LiveCovariateContext,
    LiveCovariateMemberValues,
    LiveD09MemberCrosswalk,
    build_live_covariate_context,
    capture_live_covariates,
    live_covariate_context_bytes,
    project_aggregate_covariate_summary,
    verify_live_covariate_context,
)
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistry,
    DenominatorPolicyRegistryStale,
)
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalRecord,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
    provider_measurement_id,
    provider_projection_ref,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    LongitudinalDecisionRegistryStale,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage import UnitOfAnalysis
from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    ResultCatalog,
)
from tests.test_bundles import _bundle, _provenance
from tests.test_bundles import _measurement as _bundle_measurement
from tests.test_cohort_import import _authority as _method_authority
from tests.test_cohort_import import _import as _import_cohort_record
from tests.test_cohort_manifest import (
    COLLECTED,
    TIME_AXIS,
    _collection_event,
    _known_run_revision,
    _manifest,
)
from tests.test_cohort_manifest import _authority as _provider_authority
from tests.test_cohort_summary import EMPTY_DISPOSITION_POLICY, POLICIES, _policy
from tests.test_covariate_context import _value
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    _key,
    _measurement,
    _record,
)
from tests.test_longitudinal_compatibility import _policy as _anchor_policy
from tests.test_provider_linkage import _consume, _create_approval, _token
from tests.test_provider_linkage_store import _pins, _store
from tests.test_result_catalog import ALIASES
from traceback_runner.bundles import build_result_bundle


@dataclass
class Live:
    values: tuple
    store: object
    cohort_selector_id: str
    d09: DenominatorPolicyRegistry
    d03: LongitudinalDecisionRegistry
    d09_receipt: object
    d03_receipt: object
    member_record: LongitudinalRecord
    anchor_record: LongitudinalRecord
    anchor_included: bool = False

    @property
    def cohort_registry(self) -> CohortRegistry:
        return self.values[11]

    @property
    def catalog(self) -> CohortRecordCatalog:
        return self.values[0]

    @property
    def member_result_sha256(self) -> str:
        return self.member_record.measurement.result_sha256

    @property
    def anchor_result_sha256(self) -> str:
        return self.anchor_record.measurement.result_sha256

    @property
    def policy_sha256(self) -> str:
        return longitudinal_anchor_policy_sha256(_anchor_policy(self.anchor_record))


def _shared_record(
    ref, digit: str, *, approval: str = "c", **lineage: str
) -> LongitudinalRecord:
    """A D03 record for the exact D06 catalog result, on the D05 member's linkage."""

    measurement = _measurement(digit).model_copy(
        update={"result_id": ref.result_id, "bundle_sha256": ref.bundle_sha256}
    )
    revision = _known_run_revision(
        measurement=provider_measurement_id(measurement),
        source=provider_projection_ref(measurement),
        **lineage,
    )
    authorized, _ = _consume(revision, (_create_approval(revision, approval),))
    return LongitudinalRecord(
        measurement=measurement,
        comparison_key=_key(measurement, change_digit=digit),
        linkage_revision=revision,
        authorized_linkage=authorized,
        activation_receipt=None,
    )


def _cohort_member(revision, receipt, *, collected_at=None):
    return build_cohort_member(
        revision=revision,
        receipt=receipt,
        collection_event=_collection_event(
            provider_namespace=revision.provider_namespace,
            subject_token=revision.biological.subject_token,
            collection_token=revision.biological.collection_token,
            **({} if collected_at is None else {"collected_at": collected_at}),
        ),
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )


def make_live(
    tmp_path: Path, *, anchor_included: bool = False
) -> tuple[Live, list[object]]:
    """One D05/D06/D09 cohort and one D03 series that share D01 and one result.

    The D06-imported result is the D03 series member, and both sides bind the
    same committed D01 linkage revision, so the live crosswalk can match them.
    Every linkage revision is committed before the manifest and the import
    because D05 and D06 bind the exact linkage snapshot.

    With ``anchor_included`` the D03 anchor is an ordinary D06-imported
    biological draw (one day earlier) that D09 also includes, as in a real
    cohort: the population is then the anchor plus the decided member.
    """

    closers: list[object] = []
    store = _store(tmp_path / "protected")
    closers.append(store)
    registry, head, head_sha256, capability = _method_authority()
    import_root = tmp_path / "imports"
    import_root.mkdir(parents=True)
    bundle, key, trust = _bundle(
        import_root / "incoming",
        method={
            "method_id": capability.method_ref.method_id,
            "version": capability.method_ref.version,
            "method_definition_sha256": capability.method_definition_sha256,
        },
    )
    if anchor_included:
        build_result_bundle(
            import_root / "anchor" / "record",
            measurement=_bundle_measurement(),
            provenance=_provenance(run_token="synthetic.run.anchor"),
            method={
                "method_id": capability.method_ref.method_id,
                "version": capability.method_ref.version,
                "method_definition_sha256": capability.method_definition_sha256,
            },
            signing_key=key,
        )
    # A throwaway catalog yields the exact result identity before the real
    # import, so the D03 record can name it.
    probe = ResultCatalog(
        tmp_path / "probe-results",
        import_roots={"root_primary": import_root},
        trust_store=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        ref = probe.import_bundle(
            root_id="root_primary",
            relative_path="incoming/record",
            registry=registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
            aliases=ALIASES,
        )
        anchor_ref = (
            probe.import_bundle(
                root_id="root_primary",
                relative_path="anchor/record",
                registry=registry,
                authority_head=head,
                expected_authority_head_sha256=head_sha256,
                capability=capability,
                aliases=ALIASES.model_copy(
                    update={
                        "display_alias": "dsp_dddddddd",
                        "run_alias": "rnx_eeeeeeee",
                        "timepoint_alias": "tpt_ffffffff",
                    }
                ),
            )
            if anchor_included
            else None
        )
    finally:
        probe.close()
    member_record = _shared_record(ref, "2")
    anchor_record = (
        _shared_record(
            anchor_ref,
            "1",
            approval="d",
            linkage_id=_token("linkage", "d"),
            collection=_token("collection", "d"),
            specimen=_token("specimen", "d"),
            analysis=_token("analysis", "d"),
            run_digit="d",
        )
        if anchor_included
        else _record("1")
    )
    for record in (anchor_record, member_record):
        store.commit_authorized_revision(record.authorized_linkage)
    snapshot = store.active_snapshot()
    receipts = {item.linkage_id: item for item in snapshot.receipts}
    revisions = {item.linkage_id: item for item in snapshot.revisions}
    anchor_record, member_record = (
        record.model_copy(
            update={"activation_receipt": receipts[record.linkage_revision.linkage_id]}
        )
        for record in (anchor_record, member_record)
    )
    linkage_id = member_record.linkage_revision.linkage_id
    member = _cohort_member(revisions[linkage_id], receipts[linkage_id])
    cohort_members: tuple[object, ...] = (member,)
    anchor_member = None
    if anchor_included:
        anchor_linkage_id = anchor_record.linkage_revision.linkage_id
        anchor_member = _cohort_member(
            revisions[anchor_linkage_id],
            receipts[anchor_linkage_id],
            collected_at=COLLECTED - timedelta(days=1),
        )
        cohort_members = (anchor_member, member)
    anchor = MeasurementAnchor(
        measurement_definition_sha256=capability.method_definition_sha256,
        anchor_definition_sha256="9" * 64,
        authority_sha256="a" * 64,
    )
    manifest = _manifest(
        _provider_authority(snapshot), cohort_members, measurement_anchor=anchor
    ).model_copy(update={"policies": POLICIES})
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": import_root},
        trust_store=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(results)
    cohort_registry = CohortRegistry(
        tmp_path / "cohort-registry",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(cohort_registry)
    cohorts = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=store,
        cohort_registry=cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(cohorts)
    values = (
        cohorts,
        results,
        manifest,
        member,
        bundle,
        key,
        trust,
        registry,
        head,
        head_sha256,
        capability,
        cohort_registry,
    )
    cohort_registry.register(manifest)
    selector = cohort_registry.list_selectors().records[0]
    d09 = DenominatorPolicyRegistry(
        tmp_path / "d09", cohort_registry=cohort_registry, record_catalog=cohorts
    )
    closers.append(d09)
    d03 = LongitudinalDecisionRegistry(
        tmp_path / "d03",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(d03)
    d09_receipt = d09.register_policy(
        selector.selector_id,
        selector.cohort_version,
        _policy(
            inclusion_sha256=manifest.policies.inclusion_sha256,
            exclusion_sha256=manifest.policies.exclusion_sha256,
            missingness_sha256=manifest.policies.missingness_sha256,
        ),
        EMPTY_DISPOSITION_POLICY,
        expected_cohort_manifest_sha256=cohort_manifest_sha256(manifest),
    )
    policy = _anchor_policy(anchor_record)
    d03_receipt = d03.register_series(
        anchor_record,
        (member_record,),
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
    )
    _import_cohort_record(values, selection=(selector.selector_id, 1))
    if anchor_member is not None:
        _import_cohort_record(
            values,
            selection=(selector.selector_id, 1),
            analysis_record_id=anchor_member.analysis_record_id,
            relative_path="anchor/record",
        )
    live = Live(
        values=values,
        store=store,
        cohort_selector_id=selector.selector_id,
        d09=d09,
        d03=d03,
        d09_receipt=d09_receipt,
        d03_receipt=d03_receipt,
        member_record=member_record,
        anchor_record=anchor_record,
        anchor_included=anchor_included,
    )
    return live, closers


@pytest.fixture
def live(tmp_path: Path):
    value, closers = make_live(tmp_path)
    try:
        yield value
    finally:
        for item in reversed(closers):
            item.close()


def covariates(
    live: Live,
    *,
    protocol: str = "2",
    member: str | None = None,
    anchor_batch: str = "1",
):
    member_values = (
        LiveCovariateMemberValues(
            member_sha256=member or live.member_result_sha256,
            values=(
                _value(CovariateDimension.BATCH, "1"),
                _value(CovariateDimension.PROTOCOL, protocol),
                _value(CovariateDimension.PREANALYTICS, "3"),
            ),
        ),
    )
    if not live.anchor_included:
        return member_values
    return (
        LiveCovariateMemberValues(
            member_sha256=live.anchor_result_sha256,
            values=(
                _value(CovariateDimension.BATCH, anchor_batch),
                _value(CovariateDimension.PROTOCOL, protocol),
                _value(CovariateDimension.PREANALYTICS, "3"),
            ),
        ),
        *member_values,
    )


def build(live: Live, values=None, **overrides) -> LiveCovariateContext:
    arguments = {
        "d09_registry": live.d09,
        "d09_selector_id": live.d09_receipt.selector_id,
        "d09_policy_version": 1,
        "decision_registry": live.d03,
        "series_selector_id": live.d03_receipt.selector_id,
        "expected_d02_anchor_policy_sha256": live.policy_sha256,
    }
    arguments.update(overrides)
    return build_live_covariate_context(
        covariates(live) if values is None else values, **arguments
    )


def _advance_linkage(live: Live) -> None:
    extra = _record("4")
    live.store.commit_authorized_revision(extra.authorized_linkage)


def test_live_context_uses_the_d09_population_and_d03_registry(live: Live) -> None:
    result = build(live)
    population = live.d09.resolve_population(live.d09_receipt.selector_id, 1)
    series = live.d03.resolve(live.d03_receipt.selector_id)
    (decision,) = series.decision.decisions
    (included,) = population.members.included_members

    assert result.live_d09_registry_verified is True
    assert result.d03_authority_verified is True
    # The wrapped v1 result is unchanged and still cannot prove its sources.
    assert result.context.live_d09_registry_verified is False
    assert result.context.d03_authority_verified is False
    context = result.context
    assert context.included_member_sha256s == (live.member_result_sha256,)
    assert context.d09_status_sha256 == live.d09_receipt.object_sha256
    assert (
        context.d09_population_sha256
        == population.summary.summary.population.population_sha256
    )
    assert context.cohort_manifest_sha256 == live.d09_receipt.cohort_manifest_sha256
    (member,) = context.member_contexts
    assert member.d03_decision_sha256 == longitudinal_member_decision_sha256(decision)
    assert member.d03_outcome is decision.outcome
    # The biological timepoint is derived from the D05 member, not declared.
    assert member.biological_timepoint_sha256 == hashlib.sha256(
        b"traceback-d10-biological-timepoint-v1\0"
        + included.member.biological_timepoint_id.encode("ascii")
    ).hexdigest()
    binding = result.d09_population
    assert (binding.registry_id, binding.selector_id, binding.object_sha256) == (
        live.d09_receipt.registry_id,
        live.d09_receipt.selector_id,
        live.d09_receipt.object_sha256,
    )
    (row,) = binding.included_members
    assert row.result_id == included.catalog_result.result_id
    assert row.d03_result_sha256 == live.member_result_sha256
    assert binding.linkage_snapshot_sha256 == series.decision.linkage_snapshot_sha256
    assert result.d03_series.selector_id == live.d03_receipt.selector_id
    assert live_covariate_context_bytes(result)
    aggregate = project_aggregate_covariate_summary(result.context)
    assert aggregate.included_member_count == 1
    assert aggregate.classification is CovariateClassification.CLEAR


def test_covariates_must_cover_exactly_the_d09_included_members(live: Live) -> None:
    with pytest.raises(ValueError, match="every and only D09 included member"):
        build(live, ())
    extra = covariates(live) + covariates(live, member="f" * 64)
    with pytest.raises(ValueError, match="every and only D09 included member"):
        build(live, extra)
    with pytest.raises(ValueError, match="every and only D09 included member"):
        build(live, covariates(live, member="f" * 64))


def test_d09_exclusion_empties_the_population_rather_than_widening_it(
    live: Live,
) -> None:
    # Revoking the result key withholds the member in D06, so D09 includes no
    # one; D10 derives an empty population and rejects any covariates.
    live.values[6].revoke(live.values[5].key_id)
    with pytest.raises(ValueError, match="every and only D09 included member"):
        build(live)
    empty = build(live, ())
    assert empty.context.included_member_sha256s == ()
    assert empty.context.classification is CovariateClassification.MISSING_METADATA


def test_anchor_policy_pin_must_match_the_d03_series(live: Live) -> None:
    with pytest.raises(ValueError, match="D02 anchor policy"):
        build(live, expected_d02_anchor_policy_sha256="e" * 64)
    with pytest.raises(ValueError, match="D02 anchor policy"):
        build(live, expected_d02_anchor_policy_sha256=None)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("member_result_id", "result_" + "e" * 40),
        ("member_bundle_sha256", "e" * 64),
        ("member_linkage_revision_sha256", "e" * 64),
        ("member_linkage_receipt_sha256", "e" * 64),
        ("member_linkage_receipt_sha256", None),
    ],
)
def test_crosswalk_requires_every_exact_join_key(
    live: Live, field: str, replacement: object
) -> None:
    population = live.d09.resolve_population(live.d09_receipt.selector_id, 1)
    series = live.d03.resolve(live.d03_receipt.selector_id)
    (decision,) = series.decision.decisions
    tampered = series.model_copy(
        update={
            "decision": series.decision.model_copy(
                update={"decisions": (decision.model_copy(update={field: replacement}),)}
            )
        }
    )
    with pytest.raises(ValueError, match="does not cover the D09 population"):
        covariate_module._live_from_authority(
            capture_live_covariates(covariates(live)),
            population=population,
            series=tampered,
            expected_d02_anchor_policy_sha256=live.policy_sha256,
        )
    assert covariate_module._live_from_authority(
        capture_live_covariates(covariates(live)),
        population=population,
        series=series,
        expected_d02_anchor_policy_sha256=live.policy_sha256,
    ) == build(live)


def test_d03_and_d09_reads_must_share_one_linkage_snapshot(live: Live) -> None:
    population = live.d09.resolve_population(live.d09_receipt.selector_id, 1)
    series = live.d03.resolve(live.d03_receipt.selector_id)
    for snapshot in ("e" * 64, None):
        tampered = series.model_copy(
            update={
                "decision": series.decision.model_copy(
                    update={"linkage_snapshot_sha256": snapshot}
                )
            }
        )
        with pytest.raises(ValueError, match="different linkage"):
            covariate_module._live_from_authority(
                capture_live_covariates(covariates(live)),
                population=population,
                series=tampered,
                expected_d02_anchor_policy_sha256=live.policy_sha256,
            )


def test_linkage_change_fails_build_and_verification(live: Live) -> None:
    stored = build(live)
    _advance_linkage(live)
    with pytest.raises(
        (DenominatorPolicyRegistryStale, LongitudinalDecisionRegistryStale, ValueError)
    ):
        build(live)
    with pytest.raises(
        (DenominatorPolicyRegistryStale, LongitudinalDecisionRegistryStale, ValueError)
    ):
        verify_live_covariate_context(
            stored, d09_registry=live.d09, decision_registry=live.d03
        )


def test_verify_rebuilds_and_tolerates_unrelated_registry_heads(live: Live) -> None:
    stored = build(live)
    # An unrelated D09 registration advances the D09 head only.
    live.d09.register_policy(
        live.cohort_selector_id,
        1,
        _policy(
            policy_id="denominator_research_beta",
            inclusion_sha256=live.values[2].policies.inclusion_sha256,
            exclusion_sha256=live.values[2].policies.exclusion_sha256,
            missingness_sha256=live.values[2].policies.missingness_sha256,
        ),
        EMPTY_DISPOSITION_POLICY,
        expected_cohort_manifest_sha256=live.d09_receipt.cohort_manifest_sha256,
    )
    verified = verify_live_covariate_context(
        stored, d09_registry=live.d09, decision_registry=live.d03
    )
    assert verified.context == stored.context
    assert verified.d09_population.state_version == 2
    assert stored.d09_population.state_version == 1


def test_verify_fails_when_the_d09_population_changes(live: Live) -> None:
    stored = build(live)
    live.values[6].revoke(live.values[5].key_id)
    with pytest.raises(ValueError):
        verify_live_covariate_context(
            stored, d09_registry=live.d09, decision_registry=live.d03
        )


def test_verify_rejects_a_stored_context_with_other_bindings(live: Live) -> None:
    stored = build(live)
    with pytest.raises(TypeError):
        verify_live_covariate_context(
            stored.model_copy(),  # an exact copy still verifies
            d09_registry=object(),
            decision_registry=live.d03,
        )
    values = stored.model_dump(mode="python")
    population = dict(values["d09_population"])
    population["population_sha256"] = "e" * 64
    with pytest.raises(ValueError):
        LiveCovariateContext.model_validate({**values, "d09_population": population})
    series = dict(values["d03_series"])
    series["linkage_snapshot_sha256"] = "e" * 64
    with pytest.raises(ValueError):
        LiveCovariateContext.model_validate({**values, "d03_series": series})
    crosswalk = dict(population["included_members"][0])
    crosswalk["d03_decision_sha256"] = "e" * 64
    population = dict(values["d09_population"], included_members=(crosswalk,))
    with pytest.raises(ValueError):
        LiveCovariateContext.model_validate({**values, "d09_population": population})


def test_live_path_pins_its_contract_versions(live: Live) -> None:
    result = build(live)
    encoded = live_covariate_context_bytes(result)
    assert b'"traceback.live-covariate-context.v1"' in encoded
    # v2: member rows and crosswalk rows carry a D03 role.
    assert b'"traceback.covariate-context-result.v2"' in encoded
    assert b'"traceback.d10-live-d09-population-binding.v2"' in encoded
    assert b'"traceback.d10-d03-series-binding.v1"' in encoded
    # A v1 crosswalk binding (no D03 role) is not readable as authority.
    legacy = encoded.replace(
        b"traceback.d10-live-d09-population-binding.v2",
        b"traceback.d10-live-d09-population-binding.v1",
    )
    with pytest.raises(ValueError):
        LiveCovariateContext.model_validate_json(legacy)
    # The aggregate projection still carries no member or D09 identity.
    aggregate = canonical_contract_bytes(
        project_aggregate_covariate_summary(result.context)
    )
    for secret in (
        live.member_result_sha256,
        result.d09_population.population_sha256,
        result.d09_population.included_members[0].result_id,
    ):
        assert secret.encode() not in aggregate


def test_live_covariate_capture_is_exact_and_bounded(live: Live) -> None:
    with pytest.raises(TypeError):
        capture_live_covariates(list(covariates(live)))
    with pytest.raises(ValueError, match="unique members"):
        capture_live_covariates(covariates(live) + covariates(live, protocol="4"))
    with pytest.raises(TypeError):
        capture_live_covariates(covariates(live) * 1_001)
    forged = covariates(live)[0].model_copy(update={"values": ()})
    with pytest.raises(TypeError):
        capture_live_covariates((forged,))
    with pytest.raises(TypeError):
        build(live, d09_registry=object())
    with pytest.raises(TypeError):
        build(live, decision_registry=object())


def test_build_cannot_run_inside_held_d01_or_d06_fences(live: Live) -> None:
    # Measured composition: both registry reads take the D01 linkage fence
    # themselves, so neither can run inside a caller-held D01 or D06 fence.
    store = live.store
    with type(store).authority_read_fence(store):
        with pytest.raises(DenominatorPolicyRegistryStale):
            build(live)
    with type(live.catalog).record_status_authority_fence(
        live.catalog, live.cohort_selector_id, 1, expected_registry=live.cohort_registry
    ):
        with pytest.raises(DenominatorPolicyRegistryStale):
            build(live)
    assert build(live).live_d09_registry_verified is True


def test_build_composes_inside_held_d09_and_d03_registry_locks(live: Live) -> None:
    # The D09 and D03 registry locks are reentrant in-process shared locks, so
    # a caller holding either can still build; they are never taken in the
    # reverse order by D05/D06/D01 code.
    with type(live.d09)._lock(live.d09, exclusive=False):
        assert build(live).live_d09_registry_verified is True
    with type(live.d03)._lock(live.d03, exclusive=False):
        assert build(live).live_d09_registry_verified is True


def test_concurrent_linkage_writer_never_yields_a_mixed_snapshot(live: Live) -> None:
    errors: list[BaseException] = []
    results: list[LiveCovariateContext] = []

    def reader() -> None:
        for _ in range(3):
            try:
                results.append(build(live))
            except (
                DenominatorPolicyRegistryStale,
                LongitudinalDecisionRegistryStale,
                ValueError,
            ) as error:
                errors.append(error)

    worker = threading.Thread(target=reader)
    worker.start()
    _advance_linkage(live)
    worker.join(timeout=60)
    assert not worker.is_alive()
    for result in results:
        assert (
            result.d03_series.linkage_snapshot_sha256
            == result.d09_population.linkage_snapshot_sha256
        )


@pytest.fixture
def anchored(tmp_path: Path):
    value, closers = make_live(tmp_path, anchor_included=True)
    try:
        yield value
    finally:
        for item in reversed(closers):
            item.close()


def _from_authority(live: Live, series, population=None):
    return covariate_module._live_from_authority(
        capture_live_covariates(covariates(live)),
        population=(
            live.d09.resolve_population(live.d09_receipt.selector_id, 1)
            if population is None
            else population
        ),
        series=series,
        expected_d02_anchor_policy_sha256=live.policy_sha256,
    )


def _with_series(series, **updates):
    return series.model_copy(
        update={"decision": series.decision.model_copy(update=updates)}
    )


def test_included_anchor_is_an_anchor_row_with_no_decision(anchored: Live) -> None:
    live = anchored
    population = live.d09.resolve_population(live.d09_receipt.selector_id, 1)
    series = live.d03.resolve(live.d03_receipt.selector_id)
    # D03 decides only the member; there is no reflexive anchor decision.
    (decision,) = series.decision.decisions
    assert series.decision.member_result_ids == (
        live.member_record.measurement.result_id,
    )
    assert len(population.members.included_members) == 2

    result = build(live, covariates(live, anchor_batch="4"))
    context = result.context
    assert context.included_member_sha256s == tuple(
        sorted((live.anchor_result_sha256, live.member_result_sha256))
    )
    rows = {item.member_sha256: item for item in context.member_contexts}
    anchor = rows[live.anchor_result_sha256]
    assert anchor.d03_role is D03Role.ANCHOR
    assert (anchor.d03_decision_sha256, anchor.d03_outcome) == (None, None)
    member = rows[live.member_result_sha256]
    assert member.d03_role is D03Role.MEMBER
    assert member.d03_decision_sha256 == longitudinal_member_decision_sha256(decision)
    # The anchor's own D05 timepoint and covariates take part in the context.
    included = {
        item.catalog_result.result_id: item
        for item in population.members.included_members
    }
    assert anchor.biological_timepoint_sha256 == hashlib.sha256(
        b"traceback-d10-biological-timepoint-v1\0"
        + included[
            live.anchor_record.measurement.result_id
        ].member.biological_timepoint_id.encode("ascii")
    ).hexdigest()
    assert anchor.biological_timepoint_sha256 != member.biological_timepoint_sha256
    assert context.classification is CovariateClassification.MIXED
    crosswalk = {
        item.result_id: item for item in result.d09_population.included_members
    }
    anchor_row = crosswalk[live.anchor_record.measurement.result_id]
    assert anchor_row.d03_role is D03Role.ANCHOR
    assert anchor_row.d03_result_sha256 == series.decision.anchor_result_sha256
    assert anchor_row.d03_decision_sha256 is None
    member_row = crosswalk[live.member_record.measurement.result_id]
    assert member_row.d03_role is D03Role.MEMBER
    assert member_row.d03_decision_sha256 == member.d03_decision_sha256
    assert project_aggregate_covariate_summary(context).included_member_count == 2
    assert verify_live_covariate_context(
        result, d09_registry=live.d09, decision_registry=live.d03
    ) == build(live, covariates(live, anchor_batch="4"))
    # Same batch everywhere: the anchor joins the member's group.
    assert build(live).context.classification is CovariateClassification.CLEAR


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("anchor_bundle_sha256", "e" * 64),
        ("anchor_linkage_revision_sha256", "e" * 64),
        ("anchor_linkage_receipt_sha256", "e" * 64),
        ("anchor_linkage_receipt_sha256", None),
    ],
)
def test_anchor_admission_requires_the_series_anchor_identity(
    anchored: Live, field: str, replacement: object
) -> None:
    series = anchored.d03.resolve(anchored.d03_receipt.selector_id)
    with pytest.raises(ValueError, match="anchor does not match D09"):
        _from_authority(anchored, _with_series(series, **{field: replacement}))
    assert _from_authority(anchored, series) == build(anchored)


@pytest.mark.parametrize(
    "field", ["committed_receipt_sha256", "linkage_revision_sha256"]
)
def test_anchor_admission_requires_the_d05_member_linkage(
    anchored: Live, field: str
) -> None:
    live = anchored
    series = live.d03.resolve(live.d03_receipt.selector_id)
    population = live.d09.resolve_population(live.d09_receipt.selector_id, 1)
    anchor_id = live.anchor_record.measurement.result_id
    tampered = population.model_copy(
        update={
            "members": population.members.model_copy(
                update={
                    "included_members": tuple(
                        item.model_copy(
                            update={
                                "member": item.member.model_copy(
                                    update={field: "e" * 64}
                                )
                            }
                        )
                        if item.catalog_result.result_id == anchor_id
                        else item
                        for item in population.members.included_members
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="anchor does not match D09"):
        _from_authority(live, series, tampered)


def test_a_non_anchor_member_without_a_decision_still_raises(anchored: Live) -> None:
    live = anchored
    series = live.d03.resolve(live.d03_receipt.selector_id)
    (decision,) = series.decision.decisions
    orphaned = _with_series(
        series,
        decisions=(
            decision.model_copy(update={"member_result_id": "result_" + "e" * 40}),
        ),
    )
    with pytest.raises(ValueError, match="does not cover the D09 population"):
        _from_authority(live, orphaned)
    # A series anchored elsewhere leaves the included anchor undecided too.
    elsewhere = _with_series(series, anchor_result_id="result_" + "e" * 40)
    with pytest.raises(ValueError, match="does not cover the D09 population"):
        _from_authority(live, elsewhere)
    # A series that also decides its own anchor is ambiguous and refused.
    reflexive = _with_series(
        series,
        decisions=(
            decision,
            decision.model_copy(
                update={"member_result_id": series.decision.anchor_result_id}
            ),
        ),
    )
    with pytest.raises(ValueError, match="decides its own anchor"):
        _from_authority(live, reflexive)


def test_anchor_role_must_agree_between_context_and_crosswalk(anchored: Live) -> None:
    result = build(anchored)
    for row in result.d09_population.included_members:
        fields = row.model_dump()
        flipped = {
            **fields,
            "d03_role": (
                D03Role.MEMBER if row.d03_role is D03Role.ANCHOR else D03Role.ANCHOR
            ),
        }
        with pytest.raises(ValueError, match="only the D03 anchor row omits"):
            LiveD09MemberCrosswalk.model_validate(flipped)
    values = result.model_dump(mode="python")
    population = values["d09_population"]
    rows = [dict(item) for item in population["included_members"]]
    for row in rows:
        if row["d03_role"] is D03Role.ANCHOR:
            row["d03_role"] = D03Role.MEMBER
            row["d03_decision_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="do not match the D09 crosswalk"):
        LiveCovariateContext.model_validate(
            {**values, "d09_population": {**population, "included_members": rows}}
        )
    rows = [
        dict(item, d03_decision_sha256=None) for item in population["included_members"]
    ]
    with pytest.raises(ValueError):
        LiveCovariateContext.model_validate(
            {**values, "d09_population": {**population, "included_members": rows}}
        )
    rows = [
        dict(item, d03_role=D03Role.ANCHOR, d03_decision_sha256=None)
        for item in population["included_members"]
    ]
    with pytest.raises(ValueError, match="at most one D03 anchor"):
        LiveCovariateContext.model_validate(
            {**values, "d09_population": {**population, "included_members": rows}}
        )
