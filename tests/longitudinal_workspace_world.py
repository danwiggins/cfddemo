"""One coherent synthetic world over every merged E12 store, for D08 tests.

Members (one subject, collection-time axis):

- ``A``: biological draw on day 1, D06 imported, the approved anchor;
- ``R``: technical replicate of A's day-1 collection, no D06 record;
- ``B``: biological draw on day 2, D06 imported, D03 equivalent, D07 available;
- ``C``: biological draw on day 3, no D06 record.

Every store is real and bound to one D01 linkage store, one D05 registry, one
D06 catalog over one E04 catalog and one result-trust registry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from evidence_inspector.anchor_policy_registry import AnchorPolicyRegistry
from evidence_inspector.cohort_import import CohortRecordCatalog
from evidence_inspector.cohort_summary import (
    CohortMemberExclusionSet,
    cohort_member_exclusion_set_sha256,
)
from evidence_inspector.cohort_manifest import (
    MeasurementAnchor,
    PolicyDigests,
    MemberLineageRole,
    build_cohort_member,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.compatibility import compatibility_policy_sha256
from evidence_inspector.covariate_context import (
    CovariateDimension,
    LiveCovariateMemberValues,
)
from evidence_inspector.covariate_context_registry import CovariateContextRegistry
from evidence_inspector.denominator_policy_registry import DenominatorPolicyRegistry
from evidence_inspector.fragment_explorer import FragmentQuantity, PanelId
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalRecord,
    longitudinal_anchor_policy_sha256,
    measurement_definition_sha256,
    provider_measurement_id,
    provider_projection_ref,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.longitudinal_workspace import (
    LongitudinalMeasurementSelection,
    LongitudinalWorkspaceFilters,
    LongitudinalWorkspaceRequest,
    build_longitudinal_workspace,
)
from evidence_inspector.measurement_source_artifact_registry import (
    MeasurementSourceArtifactRegistry,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.projection_policy_registry import (
    FragmentProjectionPolicyV1,
    FragmentStatistic,
    ProjectionMeasurementBinding,
    ProjectionPolicyRegistry,
    ProjectionSelectionRule,
)
from evidence_inspector.method_registry import method_definition_sha256
from evidence_inspector.provider_linkage import (
    BiologicalLineage,
    LinkageOperation,
    LinkageReasonCode,
    LinkageRevision,
    OptionalLineageState,
    OptionalOpaqueToken,
    TechnicalLineage,
    UnitOfAnalysis,
)
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
    committed_linkage_receipt_sha256,
)
from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
    ReaderAuthorizationProfile,
    ReaderAuthorizationRegistry,
    ReaderGrantBinding,
    reader_grant_sha256,
    reader_trust_sha256,
)
from evidence_inspector.reader_authorization_synthetic import (
    synthetic_reader_grant,
    synthetic_reader_trust,
)
from evidence_inspector.record_supersession_store import (
    RecordLineageRole,
    RecordSupersessionStore,
    SupersedingRecord,
    make_record_id,
)
from evidence_inspector.repeatability_comparison import repeatability_envelope_sha256
from evidence_inspector.repeatability_comparison_registry import (
    RepeatabilityComparisonRegistry,
)
from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    ResultCatalog,
)
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from evidence_inspector.result_view_source_registry import ResultViewSourceRegistry
from tests.test_bundles import _measurement, _provenance
from tests.test_cohort_manifest import TIME_AXIS, _collection_event, _manifest
from tests.test_cohort_manifest import _authority as _provider_authority
from tests.test_cohort_summary import EMPTY_DISPOSITION_POLICY, POLICIES, _ledger
from tests.test_cohort_summary import _policy as _d09_policy
from tests.test_covariate_context import _value
from tests.test_longitudinal_compatibility import _key
from tests.test_longitudinal_compatibility import _policy as _anchor_policy
from tests.test_measurement_source_artifact_registry import _fragment_authority
from tests.test_provider_linkage import PROVIDER, _consume, _create_approval, _token
from tests.test_provider_linkage_store import _pins
from tests.test_repeatability_comparison import (
    AUTHORITY_SHA256,
    EVIDENCE_SHA256,
    PROTOCOL_SHA256,
    SIGNING_KEY,
    _envelope,
    _observation,
)
from tests.test_result_catalog import ALIASES
from tests.test_result_catalog_trust_registry import public_result_key
from tests.test_result_view_source_registry import _policy as _e06_policy
from tests.test_result_view_source_registry import _record as _e06_record
from traceback_runner.bundles import build_result_bundle
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import KeyPurpose, generate_development_keypair

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
# A sequence-like protected token seeded into a caller-asserted E06 label.
SEQUENCE_TOKEN = "ACGTTGCAACGTTGCAACGTTGCA"
SUBJECT = _token("subject", "5")
DAY = {
    name: datetime(2026, 9, day, 12, tzinfo=UTC)
    for name, day in (("1", 1), ("2", 2), ("4", 3))
}
IMPORTED = ("1", "2")


def _unknown() -> OptionalOpaqueToken:
    return OptionalOpaqueToken(state=OptionalLineageState.UNKNOWN, token=None)


def _known(prefix: str, digit: str) -> OptionalOpaqueToken:
    return OptionalOpaqueToken(
        state=OptionalLineageState.KNOWN, token=_token(prefix, digit)
    )


def linkage_revision(
    digit: str,
    *,
    collection: str,
    measurement_id: str,
    projection_ref: str,
    specimen: str | None = None,
) -> LinkageRevision:
    return LinkageRevision(
        linkage_id=_token("linkage", digit),
        provider_namespace=PROVIDER,
        revision=1,
        previous_revision_sha256=None,
        operation=LinkageOperation.CREATE,
        reason_code=LinkageReasonCode.INITIAL_PROJECTION,
        source_projection_ref=projection_ref,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
        biological=BiologicalLineage(
            subject_token=SUBJECT,
            collection_token=_token("collection", collection),
            specimen_token=_token("specimen", specimen or digit),
            aliquot=_unknown(),
        ),
        technical=TechnicalLineage(
            run=_known("run", digit),
            analysis_record_id=_token("analysis", digit),
            measurement_id=measurement_id,
            reanalysis_of=_unknown(),
        ),
        proposed_at=NOW,
    )


@dataclass
class World:
    root: Path
    linkage: ProviderLinkageStore
    history: RecordSupersessionStore
    cohort: CohortRegistry
    reader: ReaderAuthorizationRegistry
    records: CohortRecordCatalog
    results: ResultCatalog
    trust: ResultTrustRegistry
    sources: ResultViewSourceRegistry
    family: MeasurementSourceArtifactRegistry
    d03: LongitudinalDecisionRegistry
    d07: RepeatabilityComparisonRegistry
    d09: DenominatorPolicyRegistry
    d10: CovariateContextRegistry
    anchors: AnchorPolicyRegistry
    projections: ProjectionPolicyRegistry
    request: LongitudinalWorkspaceRequest
    credential: ReaderGrantBinding
    manifest: object
    longitudinal: dict[str, LongitudinalRecord]
    bindings: dict[str, object]
    capability: object
    grant: object = None
    closers: list[object] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)

    def stores(self) -> dict[str, object]:
        return {
            "reader_authorization_registry": self.reader,
            "linkage_store": self.linkage,
            "cohort_registry": self.cohort,
            "cohort_record_catalog": self.records,
            "result_catalog": self.results,
            "result_trust_registry": self.trust,
            "supersession_store": self.history,
            "anchor_policy_registry": self.anchors,
            "projection_policy_registry": self.projections,
            "result_view_source_registry": self.sources,
            "measurement_source_artifact_registry": self.family,
            "d03_decision_registry": self.d03,
            "d07_comparison_registry": self.d07,
            "d09_summary_registry": self.d09,
            "d10_context_registry": self.d10,
        }

    def build(self, request=None, credential=None, **overrides):
        values = self.stores()
        values.update(overrides)
        return build_longitudinal_workspace(
            request if request is not None else self.request,
            reader_session_credential=(
                credential if credential is not None else self.credential
            ),
            **values,
        )

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()


def make_world(
    tmp_path: Path,
    *,
    with_d10: bool = True,
    with_family: bool = True,
    with_d07: bool = True,
) -> World:
    closers: list[object] = []
    linkage = ProviderLinkageStore(
        tmp_path / "linkage",
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=AuthorityTimeSource.fixed(NOW),
    )
    closers.append(linkage)
    method_registry, head, head_sha256, capability = _fragment_authority()
    definition = next(
        item
        for item in method_registry.method_definitions
        if item.method_ref == capability.method_ref
    )
    imports = tmp_path / "imports"
    imports.mkdir()
    key = generate_development_keypair(KeyPurpose.RESULT)
    method = {
        "method_id": capability.method_ref.method_id,
        "version": capability.method_ref.version,
        "method_definition_sha256": capability.method_definition_sha256,
    }
    for digit in IMPORTED:
        build_result_bundle(
            imports / f"bundle{digit}",
            measurement=_measurement(),
            provenance=_provenance(run_token=f"synthetic.run.{digit}"),
            method=method,
            signing_key=key,
        )
    trust = ResultTrustRegistry(tmp_path / "result-trust")
    closers.append(trust)
    trust.add_key(public_result_key(key))
    trust.add_key(public_result_key(SIGNING_KEY))
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": imports},
        result_trust_registry=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(results)
    # A probe catalog yields each exact result identity before the manifest.
    probe = ResultCatalog(
        tmp_path / "probe",
        import_roots={"root_primary": imports},
        result_trust_registry=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    refs = {}
    digests = {}
    try:
        for digit in IMPORTED:
            ref = probe.import_bundle(
                root_id="root_primary",
                relative_path=f"bundle{digit}",
                registry=method_registry,
                authority_head=head,
                expected_authority_head_sha256=head_sha256,
                capability=capability,
                aliases=ALIASES.model_copy(
                    update={
                        "display_alias": f"dsp_{digit * 8}",
                        "run_alias": f"rnx_{digit * 8}",
                        "timepoint_alias": f"tpt_{digit * 8}",
                    }
                ),
            )
            bundle, _ = probe.verify_reference(ref)
            refs[digit] = ref
            digests[digit] = hashlib.sha256(
                canonical_json_bytes(bundle.measurement)
            ).hexdigest()
    finally:
        probe.close()
    verified = {
        digit: _e06_record(
            SimpleNamespace(result=refs[digit]),
            definition,
            capability,
            result_sha256=digests[digit],
        )
        for digit in IMPORTED
    }
    drafts = {}
    for digit in IMPORTED:
        record = verified[digit]
        revision = linkage_revision(
            digit,
            collection=digit,
            measurement_id=provider_measurement_id(record),
            projection_ref=provider_projection_ref(record),
        )
        authorized, _ = _consume(revision, (_create_approval(revision, digit),))
        drafts[digit] = LongitudinalRecord(
            measurement=record,
            comparison_key=_key(record, change_digit=digit),
            linkage_revision=revision,
            authorized_linkage=authorized,
            activation_receipt=None,
        )
    plain = {
        "3": linkage_revision(
            "3",
            collection="1",
            specimen="1",
            measurement_id=_token("measurement", "3"),
            projection_ref=_token("projection", "3"),
        ),
        "4": linkage_revision(
            "4",
            collection="4",
            measurement_id=_token("measurement", "4"),
            projection_ref=_token("projection", "4"),
        ),
    }
    for digit in IMPORTED:
        linkage.commit_authorized_revision(drafts[digit].authorized_linkage)
    for digit, revision in plain.items():
        authorized, _ = _consume(revision, (_create_approval(revision, digit),))
        linkage.commit_authorized_revision(authorized)
    snapshot = linkage.active_snapshot()
    receipts = {item.linkage_id: item for item in snapshot.receipts}
    revisions = {item.linkage_id: item for item in snapshot.revisions}
    longitudinal = {
        digit: draft.model_copy(
            update={"activation_receipt": receipts[draft.linkage_revision.linkage_id]}
        )
        for digit, draft in drafts.items()
    }
    members = []
    for digit, collection, role, contributes in (
        ("1", "1", MemberLineageRole.BIOLOGICAL_DRAW, True),
        ("3", "1", MemberLineageRole.TECHNICAL_REPLICATE, False),
        ("2", "2", MemberLineageRole.BIOLOGICAL_DRAW, True),
        ("4", "4", MemberLineageRole.BIOLOGICAL_DRAW, True),
    ):
        linkage_id = _token("linkage", digit)
        members.append(
            build_cohort_member(
                revision=revisions[linkage_id],
                receipt=receipts[linkage_id],
                collection_event=_collection_event(
                    subject_token=SUBJECT,
                    collection_token=_token("collection", collection),
                    collected_at=DAY[collection],
                ),
                time_axis=TIME_AXIS,
                lineage_role=role,
                denominator_contribution=contributes,
                unit_of_analysis=UnitOfAnalysis.COLLECTION,
                technical_replicate_of=(
                    _token("analysis", "1")
                    if role is MemberLineageRole.TECHNICAL_REPLICATE
                    else None
                ),
            )
        )
    anchor = MeasurementAnchor(
        measurement_definition_sha256=capability.method_definition_sha256,
        anchor_definition_sha256="9" * 64,
        authority_sha256="a" * 64,
    )
    members = sorted(
        members,
        key=lambda m: (m.time_coordinate, m.provider_namespace, m.linkage_id),
    )
    # The anchor is policy-excluded from the D09 population: D10 can only
    # cover D09-included members that have a D03 member decision, and a D03
    # series never decides its own anchor.  D09 is not a comparison gate.
    exclusion = CohortMemberExclusionSet(
        member_sha256s=(
            hashlib.sha256(canonical_contract_bytes(members[0])).hexdigest(),
        )
    )
    policies = PolicyDigests(
        inclusion_sha256=POLICIES.inclusion_sha256,
        exclusion_sha256=cohort_member_exclusion_set_sha256(exclusion),
        missingness_sha256=POLICIES.missingness_sha256,
    )
    disposition = EMPTY_DISPOSITION_POLICY.model_copy(update={"exclusion": exclusion})
    manifest = _manifest(
        _provider_authority(snapshot), tuple(members), measurement_anchor=anchor
    ).model_copy(update={"policies": policies})
    cohort = CohortRegistry(
        tmp_path / "cohort-registry",
        linkage_store=linkage,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(cohort)
    cohort.register(manifest)
    selector = cohort.list_selectors().records[0]
    records = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=linkage,
        cohort_registry=cohort,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(records)
    bindings = {}
    for digit in IMPORTED:
        bindings[digit] = records.import_bundle(
            selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            provider_namespace=PROVIDER,
            analysis_record_id=_token("analysis", digit),
            root_id="root_primary",
            relative_path=f"bundle{digit}",
            registry=method_registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
        )
        assert bindings[digit].result.result_id == refs[digit].result_id
    # D04: one primary-analysis record per imported result.
    history = RecordSupersessionStore(tmp_path / "d04", linkage_store=linkage)
    closers.append(history)
    active = linkage.active_snapshot()
    for digit in IMPORTED:
        index = next(
            i
            for i, item in enumerate(active.revisions)
            if item.linkage_id == _token("linkage", digit)
        )
        revision = active.revisions[index]
        receipt = active.activation_receipts[index]
        ref = bindings[digit].result
        result_sha256 = digests[digit]
        history.commit_record(
            SupersedingRecord(
                record_id=make_record_id(
                    provider_namespace=PROVIDER,
                    analysis_record_id=revision.technical.analysis_record_id,
                    result_id=ref.result_id,
                    result_sha256=result_sha256,
                    bundle_sha256=ref.bundle_sha256,
                ),
                provider_namespace=PROVIDER,
                analysis_record_id=revision.technical.analysis_record_id,
                result_id=ref.result_id,
                result_sha256=result_sha256,
                bundle_sha256=ref.bundle_sha256,
                linkage_id=revision.linkage_id,
                linkage_revision=revision.revision,
                linkage_revision_sha256=receipt.linkage_revision_sha256,
                activation_receipt_sha256=committed_linkage_receipt_sha256(receipt),
                lineage_role=RecordLineageRole.PRIMARY_ANALYSIS,
            )
        )
    # E06 sources and E07 fragment artifacts.
    sources = ResultViewSourceRegistry(tmp_path / "e06", record_catalog=records)
    closers.append(sources)
    family = MeasurementSourceArtifactRegistry(
        tmp_path / "family", result_view_source_registry=sources
    )
    closers.append(family)
    for digit, other in (("1", "2"), ("2", "1")):
        policy = _e06_policy(verified[digit])
        receipt = sources.register_source(
            cohort_selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            record=verified[digit],
            counterpart_record=verified[other],
            policy=policy,
            expected_policy_sha256=compatibility_policy_sha256(policy),
            denominator=_ledger(),
            accessible_label="Research aggregate",
            qc_label=f"Qualified research result {SEQUENCE_TOKEN}",
        )
        if with_family:
            family.register_fragment_artifact(
                e06_selector_id=receipt.selector_id,
                e06_source_version=receipt.source_version,
                expected_member_sha256=bindings[digit].member_sha256,
                expected_result_id=bindings[digit].result.result_id,
                counterpart_record=verified[other],
                policy=policy,
            )
    # Projection policy: canonical-all E07 counts on panel A.
    digest = method_definition_sha256(definition)
    projections = ProjectionPolicyRegistry(tmp_path / "projections")
    closers.append(projections)
    projection_receipt = projections.register_policy(
        FragmentProjectionPolicyV1(
            policy_id="projpol_fragment_span",
            version=1,
            measurement=ProjectionMeasurementBinding(
                method_definition=definition,
                method_ref=definition.method_ref,
                method_definition_sha256=digest,
                quantity_id=definition.quantity_id,
                unit=definition.unit,
                measurement_definition_sha256=measurement_definition_sha256(
                    definition.method_ref,
                    digest,
                    definition.quantity_id,
                    definition.unit,
                ),
            ),
            measurement_anchor=anchor,
            fragment_quantity=FragmentQuantity.ALIGNED_REFERENCE_SPAN,
            panel=PanelId.A,
            selection_rule=ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS,
            all_component_statistics=(FragmentStatistic.COUNT,),
        )
    )
    # Anchor approval (D03 policy + D07 envelope) with candidate A.
    anchor_record = longitudinal["1"]
    member_record = longitudinal["2"]
    d03_policy = _anchor_policy(anchor_record)
    envelope = _envelope(anchor_record)
    authority_head = capability.authority_head_sha256
    anchors = AnchorPolicyRegistry(
        tmp_path / "anchors",
        linkage_store=linkage,
        cohort_registry=cohort,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(anchors)
    approval = anchors.register_policy(
        selector.selector_id,
        1,
        d03_policy,
        envelope,
        (anchor_record,),
        approval_version=1,
        expected_cohort_manifest_sha256=cohort_manifest_sha256(manifest),
        expected_policy_sha256=longitudinal_anchor_policy_sha256(d03_policy),
        expected_envelope_sha256=repeatability_envelope_sha256(envelope),
        expected_authority_head_sha256=authority_head,
    )
    page = anchors.derive_candidate_page(
        approval.selector_id, approval.approval_version
    )
    # D03 series and D07 comparison.
    d03 = LongitudinalDecisionRegistry(
        tmp_path / "d03",
        linkage_store=linkage,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(d03)
    series = d03.register_series(
        anchor_record,
        (member_record,),
        d03_policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(d03_policy),
        expected_authority_head_sha256=authority_head,
    )
    d07 = RepeatabilityComparisonRegistry(
        tmp_path / "d07",
        linkage_store=linkage,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        result_trust_registry=trust,
    )
    closers.append(d07)
    if with_d07:
        d07.register_comparison(
            anchor_record,
            member_record,
            d03_policy,
            _observation(anchor_record, 0.5),
            _observation(member_record, 0.55),
            envelope,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(d03_policy),
            expected_authority_head_sha256=authority_head,
            expected_envelope_sha256=repeatability_envelope_sha256(envelope),
            expected_evidence_sha256=EVIDENCE_SHA256,
            expected_protocol_sha256=PROTOCOL_SHA256,
            expected_repeatability_authority_sha256=AUTHORITY_SHA256,
        )
    # D09 and D10.
    d09 = DenominatorPolicyRegistry(
        tmp_path / "d09", cohort_registry=cohort, record_catalog=records
    )
    closers.append(d09)
    d09_receipt = d09.register_policy(
        selector.selector_id,
        1,
        _d09_policy(exclusion_sha256=policies.exclusion_sha256),
        disposition,
        expected_cohort_manifest_sha256=cohort_manifest_sha256(manifest),
    )
    d10 = CovariateContextRegistry(
        tmp_path / "d10", d09_registry=d09, decision_registry=d03
    )
    closers.append(d10)
    if with_d10:
        d10.register_context(
            tuple(
                LiveCovariateMemberValues(
                    member_sha256=sha,
                    values=(
                        _value(CovariateDimension.BATCH, "1"),
                        _value(CovariateDimension.PROTOCOL, "2"),
                        _value(CovariateDimension.PREANALYTICS, "3"),
                    ),
                )
                for sha in (member_record.measurement.result_sha256,)
            ),
            d09_selector_id=d09_receipt.selector_id,
            d09_policy_version=d09_receipt.policy_version,
            d03_series_selector_id=series.selector_id,
            expected_d02_anchor_policy_sha256=longitudinal_anchor_policy_sha256(
                d03_policy
            ),
        )
    # Reader registry and one synthetic grant for this cohort and measurement.
    reader_trust = synthetic_reader_trust()
    reader = ReaderAuthorizationRegistry.create(
        tmp_path / "reader",
        profile=ReaderAuthorizationProfile.SYNTHETIC,
        configured_trust=reader_trust,
        expected_trust_sha256=reader_trust_sha256(reader_trust),
        time_source=AuthorityTimeSource.fixed(NOW),
    )
    closers.append(reader)
    identity = reader.identity()
    scope = MeasurementScope(
        family=definition.family,
        quantity_id=definition.quantity_id,
        unit=definition.unit,
    )
    grant = synthetic_reader_grant(
        registry_id=identity.registry_id,
        registry_epoch_sha256=identity.registry_epoch_sha256,
        grant_selector="reader_grant_" + "1" * 32,
        cohort_registry_ids=(selector_cohort_registry_id(cohort),),
        measurement_scopes=(scope,),
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )
    receipt = reader.add_grant(grant)
    credential = ReaderGrantBinding(
        grant_sha256=reader_grant_sha256(grant),
        state_head_sha256=receipt.state_head_sha256,
    )
    request = LongitudinalWorkspaceRequest(
        cohort_selector_id=selector.selector_id,
        cohort_version=1,
        anchor_policy_selector_id=approval.selector_id,
        anchor_policy_version=approval.approval_version,
        anchor_selector_id=page.candidates[0].anchor_selector_id,
        anchor_candidate_page_sha256=page.candidate_page_sha256,
        projection_policy_selector_id=projection_receipt.selector_id,
        projection_policy_version=1,
        d09_policy_selector_id=d09_receipt.selector_id,
        d09_policy_version=d09_receipt.policy_version,
        measurement=LongitudinalMeasurementSelection(
            family=definition.family,
            quantity_id=definition.quantity_id,
            unit=definition.unit,
            measurement_definition_sha256=measurement_definition_sha256(
                definition.method_ref, digest, definition.quantity_id, definition.unit
            ),
        ),
        filters=LongitudinalWorkspaceFilters(),
    )
    return World(
        root=tmp_path,
        linkage=linkage,
        history=history,
        cohort=cohort,
        reader=reader,
        records=records,
        results=results,
        trust=trust,
        sources=sources,
        family=family,
        d03=d03,
        d07=d07,
        d09=d09,
        d10=d10,
        anchors=anchors,
        projections=projections,
        request=request,
        credential=credential,
        manifest=manifest,
        longitudinal=longitudinal,
        bindings=bindings,
        capability=capability,
        grant=grant,
        closers=closers,
        extra={
            "key": key,
            "series": series,
            "approval": approval,
            "scope": scope,
            "method_registry": method_registry,
            "head": head,
            "head_sha256": head_sha256,
            "imports": imports,
            "selector_id": selector.selector_id,
        },
    )


def selector_cohort_registry_id(cohort: CohortRegistry) -> str:
    return cohort.list_selectors(limit=1).registry_id


def protected_tokens(world: World) -> tuple[str, ...]:
    """Every protected identifier seeded into the world."""

    tokens = {PROVIDER, SUBJECT}
    for member in world.manifest.members:
        tokens.update(
            {
                member.linkage_id,
                member.subject_token,
                member.collection_token,
                member.specimen_token,
                member.analysis_record_id,
                member.run_token,
                member.biological_timepoint_id,
                member.analysis_unit_token,
                member.committed_receipt_sha256,
                member.linkage_revision_sha256,
                member.collection_event_sha256,
                member.time_coordinate_sha256,
                str(member.time_coordinate),
            }
        )
    for binding in world.bindings.values():
        tokens.update(
            {
                binding.result.result_id,
                binding.result.bundle_record_id,
                binding.result.bundle_sha256,
                binding.binding_id,
                binding.publication_id,
                binding.member_sha256,
            }
        )
    for record in world.longitudinal.values():
        tokens.update(
            {
                record.measurement.bundle_id,
                record.measurement.result_sha256,
                record.linkage_revision.technical.measurement_id,
            }
        )
    tokens.update(
        {
            str(world.root),
            "imports",
            "bundle1",
            "bundle2",
            "synthetic.run.1",
            SEQUENCE_TOKEN,
        }
    )
    return tuple(sorted(item for item in tokens if item))
