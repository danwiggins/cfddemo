"""E14 synthetic/local gate foundation tests."""

from __future__ import annotations

import base64
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from scripts.regenerate_product_gate_fixture import (
    CAPTURED_AT,
    REPO_ROOT,
    REPORT_FIXTURE,
    RUN_ID,
    run_live_report,
    structural_projection,
)
from traceback_runner.product_gates import (
    CATALOG_RECORDS,
    FILTER_P95_TARGET_US,
    STRESS_RECORDS,
    AccessibilityFixture,
    EvidenceStatus,
    ExternalRequirementId,
    ExternalRequirementState,
    FiveProviderStudyArtifact,
    GateEvidence,
    GateId,
    PinnedAuthorityHead,
    PrivacySentinelClass,
    ProductGateReport,
    ProviderTaskId,
    ProviderTaskOutcome,
    ReleaseGateAuthorityPolicy,
    ScreenshotManifest,
    SignedExternalEvidence,
    VerifiedExternalEvidence,
    _privacy_clean,
    _privacy_sentinel_probe,
    deny_external_network,
    derive_release_gate,
    load_screenshot_manifest,
)
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.signing import (
    KeyPurpose,
    SignatureEnvelope,
    TrustedKey,
    TrustNamespace,
    TrustStore,
    generate_development_keypair,
    sign_bytes,
    trusted_key_id,
)

FIXTURE = Path("tests/fixtures/product_gates/screenshot_manifest.json")


@pytest.fixture(scope="module")
def report() -> ProductGateReport:
    # Frozen output of the live harness; regenerate with
    # scripts/regenerate_product_gate_fixture.py, never by hand. The slow test
    # below checks that the live harness still matches it.
    return ProductGateReport.model_validate_json(
        (REPO_ROOT / REPORT_FIXTURE).read_bytes()
    )


@pytest.mark.slow
def test_live_harness_structure_matches_frozen_fixture(
    report: ProductGateReport,
) -> None:
    live = run_live_report()
    assert live.host_run.run_id == RUN_ID
    assert live.host_run.captured_at == CAPTURED_AT
    assert structural_projection(live.model_dump(mode="json")) == (
        structural_projection(report.model_dump(mode="json"))
    )


def test_harness_records_10k_stress_memory_and_exact_host_run(
    report: ProductGateReport,
) -> None:
    assert report.filter_performance.record_count == CATALOG_RECORDS
    assert report.initial_render.record_count == CATALOG_RECORDS
    assert report.stress_memory.record_count == STRESS_RECORDS
    assert report.stress_memory.peak_bytes > 0
    assert report.host_run.run_id == "gate_run_20260929"
    assert report.host_run.approved_host_reference is None


def test_external_gates_remain_unpassed_and_capability_disabled(
    report: ProductGateReport,
) -> None:
    evidence = {item.gate_id: item for item in report.gate_evidence}
    assert evidence[GateId.APPROVED_HOST].status == EvidenceStatus.REQUIRED_EXTERNAL
    assert (
        evidence[GateId.FIVE_PROVIDER_STUDY].status == EvidenceStatus.REQUIRED_EXTERNAL
    )
    assert evidence[GateId.ACCESSIBILITY].status == EvidenceStatus.REQUIRED_EXTERNAL
    assert evidence[GateId.SCREENSHOTS].status == EvidenceStatus.REQUIRED_EXTERNAL
    requirements = {item.requirement_id: item for item in report.external_requirements}
    assert set(requirements) == set(ExternalRequirementId)
    assert all(
        item.state == ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE
        and item.evidence_sha256 is None
        for item in requirements.values()
    )
    assert not report.capability_enabled


def test_local_service_journey_is_deterministic_and_release_stays_disabled(
    report: ProductGateReport,
) -> None:
    service = report.local_service_evidence
    evidence = {item.gate_id: item for item in report.gate_evidence}
    assert service.response_sha256 == service.repeated_response_sha256
    assert service.result_count == 100
    assert service.e12_state == "unavailable_not_implemented"
    assert not service.release_explorer_allowed
    assert not service.release_export_allowed
    assert evidence[GateId.LOCAL_SERVICE_DETERMINISM].status == (
        EvidenceStatus.OBSERVED_PASS
    )
    assert report.release_control_evidence.e12_dependency_state == (
        "blocked_unavailable_not_implemented"
    )
    assert not report.release_control_evidence.capability_enabled


def test_no_network_and_privacy_sentinel_gates_are_observed(
    report: ProductGateReport,
) -> None:
    evidence = {item.gate_id: item for item in report.gate_evidence}
    assert evidence[GateId.NO_EXTERNAL_NETWORK].status == EvidenceStatus.OBSERVED_PASS
    assert evidence[GateId.PRIVACY_SENTINELS].status == EvidenceStatus.OBSERVED_PASS
    assert {
        item.target for item in report.network_denial_evidence.observations
    } == {"192.0.2.1:443"}
    payload = canonical_json_bytes(report)
    for forbidden in (
        b"private-read-0001",
        b"raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    ):
        assert forbidden not in payload


@pytest.mark.parametrize(
    ("evidence_field", "mutation"),
    (
        (
            "privacy_sentinel_evidence",
            lambda item: item["observations"][0].__setitem__("rejected", False),
        ),
        (
            "network_denial_evidence",
            lambda item: item["observations"][0].__setitem__("denial_observed", False),
        ),
    ),
)
def test_persisted_adversarial_evidence_digest_binds_exact_results(
    report: ProductGateReport,
    evidence_field: str,
    mutation: object,
) -> None:
    payload = report.model_dump(mode="json")
    mutation(payload[evidence_field])  # type: ignore[operator]
    with pytest.raises(
        ValidationError,
        match="(?:exact observations|registered .* probes|registered probe results)",
    ):
        ProductGateReport.model_validate(payload)


def test_network_guard_blocks_socket_connections_and_restores_them() -> None:
    originals = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
    }
    with deny_external_network(allowed_loopback_ports=(443,)) as attempts:
        with pytest.raises(RuntimeError, match="network disabled"):
            socket.create_connection(("192.0.2.1", 443), timeout=0.01)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.connect_ex(("192.0.2.1", 443))
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.sendto(b"probe", ("192.0.2.1", 443))
        finally:
            probe.close()
        assert len(attempts) == 3
    assert socket.socket.connect is originals["connect"]
    assert socket.socket.connect_ex is originals["connect_ex"]
    assert socket.socket.sendto is originals["sendto"]


def test_network_guard_rejects_registered_non_loopback_before_socket_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    underlying_calls = {"connect_ex": 0, "sendto": 0}

    def unexpected_connect_ex(
        sock: socket.socket, address: tuple[str, int]
    ) -> int:
        del sock, address
        underlying_calls["connect_ex"] += 1
        raise AssertionError("guard delegated a denied connect_ex call")

    def unexpected_sendto(
        sock: socket.socket, payload: bytes, address: tuple[str, int]
    ) -> int:
        del sock, payload, address
        underlying_calls["sendto"] += 1
        raise AssertionError("guard delegated a denied sendto call")

    monkeypatch.setattr(socket.socket, "connect_ex", unexpected_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", unexpected_sendto)
    with deny_external_network() as attempts:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.connect_ex(("192.0.2.1", 443))
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.sendto(b"probe", ("192.0.2.1", 443))
        finally:
            probe.close()

    assert underlying_calls == {"connect_ex": 0, "sendto": 0}
    assert [operation for operation, _target in attempts] == [
        "connect_ex",
        "sendto",
    ]


def test_privacy_scanner_detects_every_seeded_forbidden_class() -> None:
    sentinels = (
        (PrivacySentinelClass.DONOR_IDENTIFIER, b"donor_id=private-0001"),
        (PrivacySentinelClass.READ_IDENTIFIER, b"read_id=private-read-0001"),
        (PrivacySentinelClass.ABSOLUTE_PATH, b"/Users/private/raw-input.bam"),
        (PrivacySentinelClass.RAW_SEQUENCE, b"ACGTACGTACGTACGTACGTACGTACGTACGT"),
    )
    values = tuple(value for _, value in sentinels)
    for _, sentinel in sentinels:
        assert not _privacy_clean(b"safe-prefix:" + sentinel, values)
    evidence = _privacy_sentinel_probe(
        sentinels,
        run_id="privacy_probe_run",
        host_run_sha256="a" * 64,
        output_payload_sha256="b" * 64,
        serialized_output_clean=True,
    )
    assert all(item.rejected for item in evidence.observations)


def test_screenshot_fixture_is_canonical_synthetic_accessibility_metadata() -> None:
    manifest, content = load_screenshot_manifest(FIXTURE)
    assert manifest.synthetic_only
    assert {item.zoom_percent for item in manifest.fixtures} == {100, 200}
    assert all(item.keyboard_order for item in manifest.fixtures)
    assert all(item.screen_reader_names for item in manifest.fixtures)
    assert canonical_json_bytes(manifest) == content

    payload = json.loads(content)
    payload["synthetic_only"] = False
    with pytest.raises(ValidationError):
        ScreenshotManifest.model_validate(payload)


def test_gate_contract_cannot_claim_external_pass_without_evidence() -> None:
    with pytest.raises(ValidationError, match="content-addressed evidence"):
        GateEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            status=EvidenceStatus.OBSERVED_PASS,
            detail="Unsubstantiated claim",
        )


def test_capability_cannot_be_enabled_with_required_external_gates(
    report: ProductGateReport,
) -> None:
    payload = report.model_dump(mode="json")
    payload["capability_enabled"] = True
    with pytest.raises(ValidationError, match="should be False"):
        ProductGateReport.model_validate(payload)


def _attempted_all_green_payload(report: ProductGateReport) -> dict[str, object]:
    payload = report.model_dump(mode="json")
    payload["host_run"]["approved_host_reference"] = "approved-host-1"
    host_sha256 = sha256_bytes(canonical_json_bytes(payload["host_run"]))
    payload["network_denial_evidence"]["host_run_sha256"] = host_sha256
    payload["privacy_sentinel_evidence"]["host_run_sha256"] = host_sha256
    registered_output = canonical_json_bytes(
        {
            "host_run": payload["host_run"],
            "filter": payload["filter_performance"],
            "render": payload["initial_render"],
            "memory": payload["stress_memory"],
            "screenshot_manifest_sha256": payload["screenshot_manifest_sha256"],
            "local_service_sha256": sha256_bytes(
                canonical_json_bytes(payload["local_service_evidence"])
            ),
        }
    )
    payload["privacy_sentinel_evidence"]["output_payload_sha256"] = sha256_bytes(
        registered_output
    )
    for index, item in enumerate(payload["external_requirements"], start=1):
        item["state"] = ExternalRequirementState.OBSERVED_PASS
        item["evidence_sha256"] = f"{index:064x}"
    payload["release_control_evidence"]["external_requirements_sha256"] = (
        sha256_bytes(canonical_json_bytes(payload["external_requirements"]))
    )
    payload["capability_enabled"] = False
    for index, item in enumerate(payload["gate_evidence"], start=1):
        item["status"] = EvidenceStatus.OBSERVED_PASS
        gate_id = GateId(item["gate_id"])
        if gate_id in {
            GateId.ACCESSIBILITY,
            GateId.APPROVED_HOST,
            GateId.FIVE_PROVIDER_STUDY,
            GateId.SCREENSHOTS,
        }:
            item["evidence_sha256"] = f"{index:064x}"
        elif gate_id == GateId.NO_EXTERNAL_NETWORK:
            item["evidence_sha256"] = sha256_bytes(
                canonical_json_bytes(payload["network_denial_evidence"])
            )
        elif gate_id == GateId.PRIVACY_SENTINELS:
            item["evidence_sha256"] = sha256_bytes(
                canonical_json_bytes(payload["privacy_sentinel_evidence"])
            )
    return payload


def test_foundation_rejects_self_asserted_all_green_external_evidence(
    report: ProductGateReport,
) -> None:
    with pytest.raises(
        ValidationError,
        match="(?:cannot claim observed external evidence|cannot self-assert)",
    ):
        ProductGateReport.model_validate(_attempted_all_green_payload(report))


def _verified_external(
    report: ProductGateReport, *, authority_key_id: str
) -> tuple[VerifiedExternalEvidence, ...]:
    evidence = {item.gate_id: item for item in report.gate_evidence}
    artifact_sha256 = {
        gate_id: item.evidence_sha256 or f"{index:064x}"
        for index, (gate_id, item) in enumerate(evidence.items(), start=1)
    }
    shared = {
        "authority_head_sha256": "a" * 64,
        "authority_key_id": authority_key_id,
        "verification_method": "independent-signed-artifact",
        "verified_at": datetime(2026, 9, 29, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 29, tzinfo=UTC) + timedelta(days=1),
    }
    return (
        VerifiedExternalEvidence(
            gate_id=GateId.ACCESSIBILITY,
            artifact_sha256=artifact_sha256[GateId.ACCESSIBILITY],
            keyboard_audit_passed=True,
            screen_reader_audit_passed=True,
            zoom_200_audit_passed=True,
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.APPROVED_HOST,
            artifact_sha256=artifact_sha256[GateId.APPROVED_HOST],
            approved_host_reference="approved-host-1",
            measured_run_id=report.host_run.run_id,
            host_run_sha256=sha256_bytes(canonical_json_bytes(report.host_run)),
            filter_performance_sha256=sha256_bytes(
                canonical_json_bytes(report.filter_performance)
            ),
            initial_render_sha256=sha256_bytes(
                canonical_json_bytes(report.initial_render)
            ),
            stress_memory_sha256=sha256_bytes(
                canonical_json_bytes(report.stress_memory)
            ),
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            artifact_sha256=artifact_sha256[GateId.FIVE_PROVIDER_STUDY],
            representative_users=5,
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.SCREENSHOTS,
            artifact_sha256=artifact_sha256[GateId.SCREENSHOTS],
            reviewed_browser_captures=True,
            **shared,
        ),
    )


def _authority_policy(
    evidence: tuple[VerifiedExternalEvidence, ...],
) -> ReleaseGateAuthorityPolicy:
    return ReleaseGateAuthorityPolicy(
        policy_id="release-gate-policy-1",
        issued_at=datetime(2026, 9, 28, tzinfo=UTC),
        expires_at=datetime(2026, 10, 1, tzinfo=UTC),
        pinned_heads=tuple(
            sorted(
                (
                    PinnedAuthorityHead(
                        gate_id=item.gate_id,
                        authority_key_id=item.authority_key_id,
                        authority_head_sha256=item.authority_head_sha256,
                    )
                    for item in evidence
                ),
                key=lambda item: (item.gate_id.value, item.authority_key_id),
            )
        ),
    )


def _external_signing_material() -> tuple[Ed25519PrivateKey, str, TrustStore]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = trusted_key_id(
        public_bytes,
        KeyPurpose.RELEASE,
        namespace=TrustNamespace.EXTERNAL_RELEASE,
    )
    store = TrustStore(
        (
            TrustedKey(
                key_id=key_id,
                purpose=KeyPurpose.RELEASE,
                public_key_bytes=public_bytes,
                namespace=TrustNamespace.EXTERNAL_RELEASE,
            ),
        )
    )
    return private_key, key_id, store


def _sign_external(
    evidence: tuple[VerifiedExternalEvidence, ...],
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> list[SignedExternalEvidence]:
    return [
        SignedExternalEvidence(
            evidence=item,
            signature=SignatureEnvelope(
                namespace=TrustNamespace.EXTERNAL_RELEASE,
                purpose=KeyPurpose.RELEASE,
                key_id=key_id,
                signature_base64=base64.b64encode(
                    private_key.sign(canonical_json_bytes(item))
                ).decode("ascii"),
            ),
        )
        for item in evidence
    ]


def test_release_gate_requires_independently_verified_exact_external_evidence(
    report: ProductGateReport,
) -> None:
    untrusted = derive_release_gate(report)
    assert not untrusted.capability_enabled
    assert set(untrusted.unmet_gates) == {
        GateId.ACCESSIBILITY,
        GateId.APPROVED_HOST,
        GateId.FILTER_PERFORMANCE,
        GateId.FIVE_PROVIDER_STUDY,
        GateId.INITIAL_RENDER,
        GateId.SCREENSHOTS,
        GateId.STRESS_MEMORY,
    }

    development_key = generate_development_keypair(KeyPurpose.RELEASE)
    development_store = TrustStore()
    development_store.add_signing_key(development_key)
    development_evidence = _verified_external(
        report, authority_key_id=development_key.key_id
    )
    development_signed = [
        SignedExternalEvidence(
            evidence=item,
            signature=sign_bytes(
                canonical_json_bytes(item),
                development_key,
                purpose=KeyPurpose.RELEASE,
            ),
        )
        for item in development_evidence
    ]
    self_provisioned_development = derive_release_gate(
        report,
        signed_external_evidence=development_signed,
        trust_store=development_store,
        authority_policy=_authority_policy(development_evidence),
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not self_provisioned_development.capability_enabled

    private_key, key_id, store = _external_signing_material()
    verified = _verified_external(report, authority_key_id=key_id)
    policy = _authority_policy(verified)
    signed = _sign_external(verified, private_key=private_key, key_id=key_id)
    missing_policy = derive_release_gate(
        report,
        signed_external_evidence=signed,
        trust_store=store,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not missing_policy.capability_enabled
    approved = derive_release_gate(
        report,
        signed_external_evidence=signed,
        trust_store=store,
        authority_policy=policy,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not approved.capability_enabled
    assert set(approved.unmet_gates) >= {
        GateId.ACCESSIBILITY,
        GateId.APPROVED_HOST,
        GateId.FIVE_PROVIDER_STUDY,
        GateId.SCREENSHOTS,
    }

    mismatched_head_policy = policy.model_copy(
        update={
            "pinned_heads": tuple(
                item.model_copy(update={"authority_head_sha256": "f" * 64})
                if item.gate_id == GateId.ACCESSIBILITY
                else item
                for item in policy.pinned_heads
            )
        }
    )
    head_mismatch = derive_release_gate(
        report,
        signed_external_evidence=signed,
        trust_store=store,
        authority_policy=mismatched_head_policy,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not head_mismatch.capability_enabled
    assert GateId.ACCESSIBILITY in head_mismatch.unmet_gates

    wrong_run_evidence = tuple(
        item.model_copy(update={"filter_performance_sha256": "e" * 64})
        if item.gate_id == GateId.APPROVED_HOST
        else item
        for item in verified
    )
    wrong_run_signed = _sign_external(
        wrong_run_evidence, private_key=private_key, key_id=key_id
    )
    wrong_run = derive_release_gate(
        report,
        signed_external_evidence=wrong_run_signed,
        trust_store=store,
        authority_policy=policy,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not wrong_run.capability_enabled
    assert GateId.APPROVED_HOST in wrong_run.unmet_gates

    expired = derive_release_gate(
        report,
        signed_external_evidence=signed,
        trust_store=store,
        authority_policy=policy,
        now=datetime(2026, 9, 30, tzinfo=UTC),
    )
    assert not expired.capability_enabled

    tampered = signed[0].model_copy(
        update={
            "evidence": signed[0].evidence.model_copy(
                update={"artifact_sha256": "f" * 64}
            )
        }
    )
    signed[0] = tampered
    mismatched = derive_release_gate(
        report,
        signed_external_evidence=signed,
        trust_store=store,
        authority_policy=policy,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert not mismatched.capability_enabled
    assert GateId.ACCESSIBILITY in mismatched.unmet_gates

    decision_payload = mismatched.model_dump(mode="json")
    decision_payload["capability_enabled"] = True
    with pytest.raises(ValidationError, match="derived from unmet gates"):
        type(mismatched).model_validate(decision_payload)


def test_release_gate_rejects_failed_measurement_even_with_green_status(
    report: ProductGateReport,
) -> None:
    payload = report.model_dump(mode="json")
    payload["filter_performance"]["p95_us"] = FILTER_P95_TARGET_US + 1
    payload["filter_performance"]["samples_us"] = [FILTER_P95_TARGET_US + 1] * 5
    payload["filter_performance"]["target_met"] = False
    for item in payload["gate_evidence"]:
        if item["gate_id"] == GateId.FILTER_PERFORMANCE:
            item["status"] = EvidenceStatus.OBSERVED_PASS
            item["evidence_sha256"] = sha256_bytes(
                canonical_json_bytes(payload["filter_performance"])
            )
    with pytest.raises(ValidationError, match="cannot pass a failed measurement"):
        ProductGateReport.model_validate(payload)


@pytest.mark.parametrize(
    "kwargs,match",
    (
        (
            {"gate_id": GateId.FIVE_PROVIDER_STUDY, "representative_users": 4},
            "at least five",
        ),
        (
            {"gate_id": GateId.ACCESSIBILITY, "keyboard_audit_passed": True},
            "keyboard, screen-reader",
        ),
        ({"gate_id": GateId.SCREENSHOTS}, "reviewed browser captures"),
        ({"gate_id": GateId.APPROVED_HOST}, "bind the host and exact measured run"),
    ),
)
def test_external_evidence_contracts_fail_closed(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        VerifiedExternalEvidence(
            artifact_sha256="1" * 64,
            authority_head_sha256="2" * 64,
            authority_key_id="provider-evidence-authority",
            verification_method="independent-signed-artifact",
            verified_at=datetime(2026, 9, 29, tzinfo=UTC),
            expires_at=datetime(2026, 9, 30, tzinfo=UTC),
            **kwargs,
        )


@pytest.mark.parametrize(
    "unsafe",
    (
        "../../private/raw-input.bam",
        "Open docs/../../private/raw-input.bam",
        "//evil.example/private",
        "source_id=private-0001",
        "source_identifier:private-0001",
        "path:/Volumes/private/raw-input.bam",
        "path=/private/raw-input.bam",
        "source id private-0001",
        "source identifier private-0001",
        "Patient id private-0001",
        "https:evil.example",
        "%252FVolumes%252Fprivate%252Fraw-input.bam",
        "A C G T A C G T A C G T A C G T A C G T A C G T",
        "%25252525252FVolumes%25252525252Fprivate",
        "javascript:alert(1)",
        "data:text/plain,private",
        "wss:evil.example",
        "Open(https:evil.example)",
        "Open,javascript:alert(1)",
        "URL.data:text",
        "A,C(G)T-A_C.G T,A(C)G-T_A C.G,T-A,C(G)T-A_C.GT",
    ),
)
def test_safe_operator_grammar_rejects_review_bypasses_in_gate_surfaces(
    unsafe: str,
) -> None:
    with pytest.raises(ValidationError, match="operator text"):
        GateEvidence(
            gate_id=GateId.ACCESSIBILITY,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail=unsafe,
        )
    with pytest.raises(ValidationError, match="operator text"):
        AccessibilityFixture(
            fixture_id="unsafe-fixture",
            viewport_width_px=1280,
            zoom_percent=200,
            keyboard_order=("queue",),
            screen_reader_names=(unsafe,),
            non_color_status_text=("Blocked",),
        )


def test_privacy_output_digest_cannot_be_resealed_by_caller(
    report: ProductGateReport,
) -> None:
    payload = report.model_dump(mode="json")
    payload["privacy_sentinel_evidence"]["output_payload_sha256"] = "f" * 64
    for item in payload["gate_evidence"]:
        if item["gate_id"] == GateId.PRIVACY_SENTINELS:
            item["evidence_sha256"] = sha256_bytes(
                canonical_json_bytes(payload["privacy_sentinel_evidence"])
            )
    with pytest.raises(ValidationError, match="replay the registered probes"):
        ProductGateReport.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("release_explorer_allowed", True),
        ("release_export_allowed", True),
        ("e12_state", "available"),
    ),
)
def test_local_service_cannot_upgrade_release_or_e12(
    report: ProductGateReport, field: str, value: object
) -> None:
    payload = report.model_dump(mode="json")
    payload["local_service_evidence"][field] = value
    with pytest.raises(ValidationError):
        ProductGateReport.model_validate(payload)


def _provider_study() -> FiveProviderStudyArtifact:
    outcomes = tuple(
        sorted(
            (
                ProviderTaskOutcome(
                    participant_id=f"provider-{participant}",
                    task_id=task,
                    completed=True,
                    duration_seconds=(
                        10
                        if task == ProviderTaskId.IDENTIFY_JOB_STATE_OWNER_ACTION
                        else 300
                        if task == ProviderTaskId.DOCTOR_DEMO_VERIFY
                        else 30
                    ),
                    error_count=0,
                    coaching_required=False,
                    believed_data_uploaded=False,
                )
                for participant in range(1, 6)
                for task in ProviderTaskId
            ),
            key=lambda item: (item.participant_id, item.task_id.value),
        )
    )
    return FiveProviderStudyArtifact(
        conducted_at=datetime(2026, 9, 29, tzinfo=UTC),
        protocol_sha256="1" * 64,
        browser_capture_artifact_sha256="2" * 64,
        outcomes=outcomes,
    )


def test_five_provider_artifact_enforces_frozen_tasks_and_thresholds() -> None:
    study = _provider_study()
    assert len(study.outcomes) == 25

    payload = study.model_dump(mode="json")
    identify = next(
        item
        for item in payload["outcomes"]
        if item["task_id"] == ProviderTaskId.IDENTIFY_JOB_STATE_OWNER_ACTION
    )
    identify["duration_seconds"] = 11
    with pytest.raises(ValidationError, match="within ten seconds"):
        FiveProviderStudyArtifact.model_validate(payload)

    payload = study.model_dump(mode="json")
    for item in payload["outcomes"]:
        if item["participant_id"] in {"provider-1", "provider-2"} and item[
            "task_id"
        ] == ProviderTaskId.RECOVER_LOW_DISK:
            item["coaching_required"] = True
    with pytest.raises(ValidationError, match="four participants"):
        FiveProviderStudyArtifact.model_validate(payload)

    payload = study.model_dump(mode="json")
    saved = next(
        item
        for item in payload["outcomes"]
        if item["task_id"] == ProviderTaskId.SAVE_VERIFY_WITHOUT_UPLOAD_BELIEF
    )
    saved["believed_data_uploaded"] = True
    with pytest.raises(ValidationError, match="without upload belief"):
        FiveProviderStudyArtifact.model_validate(payload)
