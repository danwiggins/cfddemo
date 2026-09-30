"""E14 synthetic/local gate foundation tests."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from traceback_runner.product_gates import (
    CATALOG_RECORDS,
    STRESS_RECORDS,
    EvidenceStatus,
    GateEvidence,
    GateId,
    ProductGateReport,
    ScreenshotManifest,
    _privacy_clean,
    deny_external_network,
    load_screenshot_manifest,
    run_foundation_gates,
)
from traceback_runner.serialization import canonical_json_bytes

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
        evidence[GateId.FIVE_PROVIDER_STUDY].status
        == EvidenceStatus.REQUIRED_EXTERNAL
    )
    assert evidence[GateId.ACCESSIBILITY].status == EvidenceStatus.FIXTURE_ONLY
    assert evidence[GateId.SCREENSHOTS].status == EvidenceStatus.FIXTURE_ONLY
    assert not report.capability_enabled


def test_no_network_and_privacy_sentinel_gates_are_observed(
    report: ProductGateReport,
) -> None:
    evidence = {item.gate_id: item for item in report.gate_evidence}
    assert (
        evidence[GateId.NO_EXTERNAL_NETWORK].status
        == EvidenceStatus.OBSERVED_PASS
    )
    assert (
        evidence[GateId.PRIVACY_SENTINELS].status
        == EvidenceStatus.OBSERVED_PASS
    )
    payload = canonical_json_bytes(report)
    for forbidden in (
        b"private-read-0001",
        b"raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    ):
        assert forbidden not in payload


def test_network_guard_blocks_socket_connections_and_restores_them() -> None:
    original = socket.socket.connect
    with deny_external_network() as attempts:
        with pytest.raises(RuntimeError, match="network disabled"):
            socket.create_connection(("192.0.2.1", 443), timeout=0.01)
        assert attempts == ["('192.0.2.1', 443)"]
    assert socket.socket.connect is original


def test_privacy_scanner_detects_every_seeded_forbidden_class() -> None:
    sentinels = (
        b"donor_id=private-0001",
        b"read_id=private-read-0001",
        b"/Users/private/raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    )
    for sentinel in sentinels:
        assert not _privacy_clean(b"safe-prefix:" + sentinel, sentinels)


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
    with pytest.raises(ValidationError, match="evidence reference"):
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
    with pytest.raises(ValidationError, match="derived"):
        ProductGateReport.model_validate(payload)
