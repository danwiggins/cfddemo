"""E14 synthetic/local gate foundation tests."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from traceback_runner.product_gates import (
    CATALOG_RECORDS,
    FILTER_P95_TARGET_US,
    STRESS_RECORDS,
    EvidenceStatus,
    GateEvidence,
    GateId,
    ProductGateReport,
    ScreenshotManifest,
    SignedExternalEvidence,
    VerifiedExternalEvidence,
    _privacy_clean,
    _privacy_sentinel_probe,
    deny_external_network,
    derive_release_gate,
    load_screenshot_manifest,
    run_foundation_gates,
)
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import (
    KeyPurpose,
    TrustStore,
    generate_development_keypair,
    sign_bytes,
)

FIXTURE = Path("tests/fixtures/product_gates/screenshot_manifest.json")


@pytest.fixture(scope="module")
def report() -> ProductGateReport:
    return run_foundation_gates(
        screenshot_manifest_path=FIXTURE,
        run_id="gate_run_20260929",
        captured_at=datetime(2026, 9, 29, tzinfo=UTC),
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
    assert evidence[GateId.ACCESSIBILITY].status == EvidenceStatus.FIXTURE_ONLY
    assert evidence[GateId.SCREENSHOTS].status == EvidenceStatus.FIXTURE_ONLY
    assert not report.capability_enabled


def test_no_network_and_privacy_sentinel_gates_are_observed(
    report: ProductGateReport,
) -> None:
    evidence = {item.gate_id: item for item in report.gate_evidence}
    assert evidence[GateId.NO_EXTERNAL_NETWORK].status == EvidenceStatus.OBSERVED_PASS
    assert evidence[GateId.PRIVACY_SENTINELS].status == EvidenceStatus.OBSERVED_PASS
    payload = canonical_json_bytes(report)
    for forbidden in (
        b"private-read-0001",
        b"raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    ):
        assert forbidden not in payload


def test_network_guard_blocks_socket_connections_and_restores_them() -> None:
    originals = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
    }
    with deny_external_network() as attempts:
        with pytest.raises(RuntimeError, match="network disabled"):
            socket.create_connection(("192.0.2.1", 443), timeout=0.01)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.connect_ex(("127.0.0.1", 9))
            with pytest.raises(RuntimeError, match="network disabled"):
                probe.sendto(b"probe", ("127.0.0.1", 9))
        finally:
            probe.close()
        assert len(attempts) == 3
    assert socket.socket.connect is originals["connect"]
    assert socket.socket.connect_ex is originals["connect_ex"]
    assert socket.socket.sendto is originals["sendto"]


def test_privacy_scanner_detects_every_seeded_forbidden_class() -> None:
    sentinels = (
        b"donor_id=private-0001",
        b"read_id=private-read-0001",
        b"/Users/private/raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    )
    for sentinel in sentinels:
        assert not _privacy_clean(b"safe-prefix:" + sentinel, sentinels)
    assert _privacy_sentinel_probe(sentinels)


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


def _all_green_report(report: ProductGateReport) -> ProductGateReport:
    payload = report.model_dump(mode="json")
    payload["host_run"]["approved_host_reference"] = "approved-host-1"
    payload["capability_enabled"] = False
    for index, item in enumerate(payload["gate_evidence"], start=1):
        item["status"] = EvidenceStatus.OBSERVED_PASS
        item["evidence_sha256"] = f"{index:064x}"
    return ProductGateReport.model_validate(payload)


def _verified_external(
    report: ProductGateReport, *, authority_key_id: str
) -> tuple[VerifiedExternalEvidence, ...]:
    evidence = {item.gate_id: item for item in report.gate_evidence}
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
            artifact_sha256=evidence[GateId.ACCESSIBILITY].evidence_sha256,
            keyboard_audit_passed=True,
            screen_reader_audit_passed=True,
            zoom_200_audit_passed=True,
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.APPROVED_HOST,
            artifact_sha256=evidence[GateId.APPROVED_HOST].evidence_sha256,
            approved_host_reference="approved-host-1",
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            artifact_sha256=evidence[GateId.FIVE_PROVIDER_STUDY].evidence_sha256,
            representative_users=5,
            **shared,
        ),
        VerifiedExternalEvidence(
            gate_id=GateId.SCREENSHOTS,
            artifact_sha256=evidence[GateId.SCREENSHOTS].evidence_sha256,
            reviewed_browser_captures=True,
            **shared,
        ),
    )


def test_release_gate_requires_independently_verified_exact_external_evidence(
    report: ProductGateReport,
) -> None:
    green = _all_green_report(report)
    untrusted = derive_release_gate(green)
    assert not untrusted.capability_enabled
    assert set(untrusted.unmet_gates) == {
        GateId.ACCESSIBILITY,
        GateId.APPROVED_HOST,
        GateId.FIVE_PROVIDER_STUDY,
        GateId.SCREENSHOTS,
    }

    key = generate_development_keypair(KeyPurpose.RELEASE)
    store = TrustStore()
    store.add_signing_key(key)
    verified = list(_verified_external(green, authority_key_id=key.key_id))
    signed = [
        SignedExternalEvidence(
            evidence=item,
            signature=sign_bytes(
                canonical_json_bytes(item), key, purpose=KeyPurpose.RELEASE
            ),
        )
        for item in verified
    ]
    missing_clock = derive_release_gate(
        green, signed_external_evidence=signed, trust_store=store
    )
    assert not missing_clock.capability_enabled
    approved = derive_release_gate(
        green,
        signed_external_evidence=signed,
        trust_store=store,
        now=datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    assert approved.capability_enabled
    assert approved.unmet_gates == ()

    expired = derive_release_gate(
        green,
        signed_external_evidence=signed,
        trust_store=store,
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
        green,
        signed_external_evidence=signed,
        trust_store=store,
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
        ({"gate_id": GateId.APPROVED_HOST}, "bind the host reference"),
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
