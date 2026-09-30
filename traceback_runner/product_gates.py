"""Offline E14 foundation harness with explicit unpassed external gates.

The harness measures synthetic catalog-shaped work on the current machine.  A
local measurement is not approved-host evidence, accessibility metadata is not
a manual accessibility audit, and fixtures are not a five-provider study.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import platform
import socket
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogQuery,
    CatalogResultRef,
    ResultCatalog,
)
from traceback_runner.contracts import RunnerContract
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.signing import (
    SignatureEnvelope,
    TrustStore,
)
from traceback_runner.store import JobStore
from traceback_runner.web.contracts import ProblemDetail, ProblemOwner, SafeText
from traceback_runner.web.server import RunningLocalWebService

CATALOG_RECORDS = 10_000
STRESS_RECORDS = 100_000
FILTER_P95_TARGET_US = 250_000
INITIAL_RENDER_TARGET_US = 2_000_000
MAX_SYNTHETIC_PEAK_BYTES = 256 * 1024 * 1024
HARNESS_VERSION = "traceback-product-gates.v3"
MAX_LOCAL_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024

SafeToken = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_SAFE_TEXT = TypeAdapter(SafeText)
_NETWORK_GUARD_LOCK = RLock()


class GateId(StrEnum):
    FILTER_PERFORMANCE = "filter_performance"
    INITIAL_RENDER = "initial_render"
    STRESS_MEMORY = "stress_memory"
    NO_EXTERNAL_NETWORK = "no_external_network"
    PRIVACY_SENTINELS = "privacy_sentinels"
    ACCESSIBILITY = "accessibility"
    SCREENSHOTS = "screenshots"
    APPROVED_HOST = "approved_host"
    FIVE_PROVIDER_STUDY = "five_provider_study"
    LOCAL_SERVICE_DETERMINISM = "local_service_determinism"


class EvidenceStatus(StrEnum):
    OBSERVED_PASS = "observed_pass"
    OBSERVED_FAIL = "observed_fail"
    OBSERVED_LOCAL_UNAPPROVED = "observed_local_unapproved"
    FIXTURE_ONLY = "fixture_only"
    REQUIRED_EXTERNAL = "required_external"


class ExternalRequirementId(StrEnum):
    APPROVED_HOST = "approved_host"
    KEYBOARD_ONLY = "keyboard_only"
    SCREEN_READER = "screen_reader"
    ZOOM_200 = "zoom_200"
    REVIEWED_SCREENSHOTS = "reviewed_screenshots"
    FIVE_PROVIDER_TASKS = "five_provider_tasks"


class ExternalRequirementState(StrEnum):
    UNMET_NO_OBSERVED_EVIDENCE = "unmet_no_observed_evidence"
    OBSERVED_FAIL = "observed_fail"
    OBSERVED_PASS = "observed_pass"


class ProviderTaskId(StrEnum):
    IDENTIFY_JOB_STATE_OWNER_ACTION = "identify_job_state_owner_action"
    RECOVER_LOW_DISK = "recover_low_disk"
    RECOVER_INTERRUPTION = "recover_interruption"
    SAVE_VERIFY_WITHOUT_UPLOAD_BELIEF = "save_verify_without_upload_belief"
    DOCTOR_DEMO_VERIFY = "doctor_demo_verify"


class PrivacySentinelClass(StrEnum):
    DONOR_IDENTIFIER = "donor_identifier"
    READ_IDENTIFIER = "read_identifier"
    ABSOLUTE_PATH = "absolute_path"
    RAW_SEQUENCE = "raw_sequence"


class PrivacyProbePath(StrEnum):
    CATALOG = "catalog"
    PROBLEM_RESPONSE = "problem_response"
    SCREENSHOT = "screenshot"


class NetworkProbeOperation(StrEnum):
    CONNECT_EX = "connect_ex"
    SENDTO = "sendto"


REGISTERED_PRIVACY_SENTINELS = {
    PrivacySentinelClass.DONOR_IDENTIFIER: b"donor_id=private-0001",
    PrivacySentinelClass.READ_IDENTIFIER: b"read_id=private-read-0001",
    PrivacySentinelClass.ABSOLUTE_PATH: b"/Users/private/raw-input.bam",
    PrivacySentinelClass.RAW_SEQUENCE: b"ACGTACGTACGTACGTACGTACGTACGTACGT",
}
REGISTERED_NETWORK_TARGET = ("192.0.2.1", 443)
REGISTERED_NETWORK_PAYLOAD = b"privacy-safe-probe"


class PrivacyPathObservation(RunnerContract):
    sentinel_class: PrivacySentinelClass
    sentinel_sha256: Sha256
    path: PrivacyProbePath
    rejected: bool


class ServicePrivacyObservation(RunnerContract):
    sentinel_class: PrivacySentinelClass
    sentinel_sha256: Sha256
    status_code: Literal[400]
    response_sha256: Sha256
    sentinel_absent: Literal[True] = True


class PrivacySentinelEvidence(RunnerContract):
    schema_version: Literal["traceback.privacy-sentinel-evidence.v2"] = (
        "traceback.privacy-sentinel-evidence.v2"
    )
    harness_version: Literal["traceback-product-gates.v3"] = HARNESS_VERSION
    run_id: SafeToken
    host_run_sha256: Sha256
    output_payload_sha256: Sha256
    serialized_output_clean: bool
    observations: tuple[PrivacyPathObservation, ...] = Field(
        min_length=12, max_length=12
    )
    local_service_observations: tuple[ServicePrivacyObservation, ...] = Field(
        min_length=4, max_length=4
    )

    @model_validator(mode="after")
    def complete_probe_matrix(self) -> PrivacySentinelEvidence:
        keys = tuple(
            (item.sentinel_class.value, item.path.value) for item in self.observations
        )
        expected = tuple(
            sorted(
                (sentinel.value, path.value)
                for sentinel in PrivacySentinelClass
                for path in PrivacyProbePath
            )
        )
        if keys != expected:
            raise ValueError(
                "privacy evidence must contain the complete sorted probe matrix"
            )
        per_class: dict[PrivacySentinelClass, set[str]] = {}
        for item in self.observations:
            per_class.setdefault(item.sentinel_class, set()).add(item.sentinel_sha256)
        if any(len(digests) != 1 for digests in per_class.values()):
            raise ValueError("each sentinel class must bind one exact sentinel digest")
        for sentinel_class, value in REGISTERED_PRIVACY_SENTINELS.items():
            if per_class.get(sentinel_class) != {sha256_bytes(value)}:
                raise ValueError("privacy evidence must bind registered sentinels")
        service_keys = tuple(
            item.sentinel_class for item in self.local_service_observations
        )
        if service_keys != tuple(sorted(PrivacySentinelClass, key=str)):
            raise ValueError(
                "privacy evidence must bind every local-service sentinel probe"
            )
        if any(
            item.sentinel_sha256
            != sha256_bytes(REGISTERED_PRIVACY_SENTINELS[item.sentinel_class])
            for item in self.local_service_observations
        ):
            raise ValueError("local-service privacy probes must bind registered sentinels")
        if not self.serialized_output_clean or not all(
            item.rejected for item in self.observations
        ):
            raise ValueError("registered privacy probes must all reject")
        return self


class NetworkProbeObservation(RunnerContract):
    operation: NetworkProbeOperation
    target: Literal["192.0.2.1:443"] = "192.0.2.1:443"
    denial_observed: bool
    guard_recorded: bool
    payload_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def payload_matches_operation(self) -> NetworkProbeObservation:
        if (self.operation == NetworkProbeOperation.SENDTO) != (
            self.payload_sha256 is not None
        ):
            raise ValueError("only sendto evidence binds the exact probe payload")
        return self


class NetworkInterceptObservation(RunnerContract):
    operation: SafeToken
    target_sha256: Sha256


class NetworkDenialEvidence(RunnerContract):
    schema_version: Literal["traceback.network-denial-evidence.v2"] = (
        "traceback.network-denial-evidence.v2"
    )
    harness_version: Literal["traceback-product-gates.v3"] = HARNESS_VERSION
    run_id: SafeToken
    host_run_sha256: Sha256
    local_service_sha256: Sha256
    observations: tuple[NetworkProbeObservation, ...] = Field(
        min_length=2, max_length=2
    )
    intercepted_attempts: tuple[NetworkInterceptObservation, ...] = Field(
        min_length=2, max_length=64
    )

    @model_validator(mode="after")
    def exact_network_probes(self) -> NetworkDenialEvidence:
        operations = tuple(item.operation for item in self.observations)
        if operations != tuple(sorted(NetworkProbeOperation, key=str)):
            raise ValueError("network evidence must bind connect_ex and sendto")
        intercepted = {item.operation for item in self.intercepted_attempts}
        if not {operation.value for operation in NetworkProbeOperation} <= intercepted:
            raise ValueError(
                "network evidence must persist both guarded probe attempts"
            )
        expected_target = sha256_bytes(repr(REGISTERED_NETWORK_TARGET).encode())
        expected = {
            NetworkProbeOperation.CONNECT_EX: None,
            NetworkProbeOperation.SENDTO: sha256_bytes(REGISTERED_NETWORK_PAYLOAD),
        }
        if any(
            not item.denial_observed
            or not item.guard_recorded
            or item.target != "192.0.2.1:443"
            or item.payload_sha256 != expected[item.operation]
            for item in self.observations
        ):
            raise ValueError("network evidence must bind registered probe results")
        if tuple(
            (item.operation, item.target_sha256) for item in self.intercepted_attempts
        ) != tuple(
            (operation.value, expected_target)
            for operation in sorted(NetworkProbeOperation, key=str)
        ):
            raise ValueError("network evidence must bind exact registered attempts")
        return self


class HostRunEvidence(RunnerContract):
    schema_version: Literal["traceback.host-run-evidence.v1"] = (
        "traceback.host-run-evidence.v1"
    )
    run_id: SafeToken
    captured_at: datetime
    python_version: SafeText
    operating_system: SafeText
    machine: SafeText
    processor: SafeText
    approved_host_reference: SafeToken | None = None


class LocalServiceEvidence(RunnerContract):
    """Observed synthetic journey through the packaged loopback HTTP service."""

    schema_version: Literal["traceback.local-service-gate-evidence.v1"] = (
        "traceback.local-service-gate-evidence.v1"
    )
    request_path: Literal["/api/v1/explorer/catalog?limit=100"] = (
        "/api/v1/explorer/catalog?limit=100"
    )
    unauthorized_status: Literal[401]
    bootstrap_status: Literal[200]
    authenticated_status: Literal[200]
    repeated_status: Literal[200]
    result_count: Literal[100]
    response_sha256: Sha256
    repeated_response_sha256: Sha256
    packaged_assets_sha256: Sha256
    privacy_rejection_response_sha256: tuple[Sha256, ...] = Field(
        min_length=4, max_length=4
    )
    deterministic: Literal[True] = True
    outbound_network_enabled: Literal[False] = False
    release_explorer_allowed: Literal[False] = False
    release_export_allowed: Literal[False] = False
    e12_state: Literal["unavailable_not_implemented"] = (
        "unavailable_not_implemented"
    )

    @model_validator(mode="after")
    def exact_local_service_result(self) -> LocalServiceEvidence:
        if self.response_sha256 != self.repeated_response_sha256:
            raise ValueError("local service response must reproduce byte-identically")
        if self.privacy_rejection_response_sha256 != tuple(
            sorted(self.privacy_rejection_response_sha256)
        ):
            raise ValueError(
                "local-service privacy response digests must be sorted"
            )
        return self


class ExternalRequirementEvidence(RunnerContract):
    requirement_id: ExternalRequirementId
    state: ExternalRequirementState
    detail: SafeText
    evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def evidence_matches_state(self) -> ExternalRequirementEvidence:
        if (
            self.state == ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE
            and self.evidence_sha256 is not None
        ):
            raise ValueError(
                "unmet external requirements cannot carry observed evidence"
            )
        if self.state != ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE and (
            self.evidence_sha256 is None
        ):
            raise ValueError("observed external status requires exact evidence")
        return self


class ReleaseControlEvidence(RunnerContract):
    schema_version: Literal["traceback.e14-release-control-evidence.v1"] = (
        "traceback.e14-release-control-evidence.v1"
    )
    local_service_sha256: Sha256
    external_requirements_sha256: Sha256
    e12_dependency_state: Literal["blocked_unavailable_not_implemented"] = (
        "blocked_unavailable_not_implemented"
    )
    release_explorer_allowed: Literal[False] = False
    release_export_allowed: Literal[False] = False
    capability_enabled: Literal[False] = False


class PerformanceMeasurement(RunnerContract):
    name: Literal["filter_sort", "initial_render"]
    record_count: int = Field(ge=1, le=STRESS_RECORDS)
    samples_us: tuple[int, ...] = Field(min_length=5, max_length=128)
    p95_us: int = Field(ge=0)
    target_us: int = Field(gt=0)
    target_met: bool

    @model_validator(mode="after")
    def derived_values_match(self) -> PerformanceMeasurement:
        ordered = sorted(self.samples_us)
        rank = max(0, ((95 * len(ordered) + 99) // 100) - 1)
        if self.p95_us != ordered[rank]:
            raise ValueError("p95 must use the nearest-rank definition")
        if self.target_met != (self.p95_us <= self.target_us):
            raise ValueError("target_met must be derived from p95")
        return self


class MemoryMeasurement(RunnerContract):
    record_count: Literal[100_000] = STRESS_RECORDS
    peak_bytes: int = Field(ge=0)
    target_bytes: Literal[268_435_456] = MAX_SYNTHETIC_PEAK_BYTES
    target_met: bool

    @model_validator(mode="after")
    def derived_target(self) -> MemoryMeasurement:
        if self.target_met != (self.peak_bytes <= self.target_bytes):
            raise ValueError("memory target must be derived from peak bytes")
        return self


class AccessibilityFixture(RunnerContract):
    fixture_id: SafeToken
    viewport_width_px: int = Field(ge=320, le=3840)
    zoom_percent: Literal[100, 200]
    keyboard_order: tuple[SafeToken, ...] = Field(min_length=1, max_length=64)
    screen_reader_names: tuple[SafeText, ...] = Field(min_length=1, max_length=64)
    non_color_status_text: tuple[SafeText, ...] = Field(min_length=1, max_length=32)


class ScreenshotManifest(RunnerContract):
    schema_version: Literal["traceback.screenshot-fixtures.v1"] = (
        "traceback.screenshot-fixtures.v1"
    )
    synthetic_only: Literal[True] = True
    fixtures: tuple[AccessibilityFixture, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_fixture_ids(self) -> ScreenshotManifest:
        ids = [item.fixture_id for item in self.fixtures]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("screenshot fixtures must be uniquely sorted")
        return self


class BrowserCapture(RunnerContract):
    """Parsed content address for one reviewed, real-browser capture."""

    capture_id: SafeToken
    fixture_id: SafeToken
    surface_state: Literal["loading", "empty", "ready", "error", "incompatible"]
    viewport_width_px: int = Field(ge=320, le=3840)
    zoom_percent: Literal[100, 200]
    image_sha256: Sha256
    dom_sha256: Sha256
    filters_sha256: Sha256


class BrowserCaptureArtifact(RunnerContract):
    schema_version: Literal["traceback.browser-capture-artifact.v1"] = (
        "traceback.browser-capture-artifact.v1"
    )
    captured_at: datetime
    browser_name: SafeToken
    browser_version: SafeText
    captures: tuple[BrowserCapture, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def canonical_captures(self) -> BrowserCaptureArtifact:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("browser capture time must be timezone-aware")
        keys = [item.capture_id for item in self.captures]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("browser captures must be uniquely sorted")
        return self


class AccessibilityAuditArtifact(RunnerContract):
    schema_version: Literal["traceback.accessibility-audit-artifact.v1"] = (
        "traceback.accessibility-audit-artifact.v1"
    )
    audited_at: datetime
    auditor_id: SafeToken
    browser_capture_artifact_sha256: Sha256
    keyboard_audit_passed: bool
    screen_reader_audit_passed: bool
    zoom_200_audit_passed: bool
    findings: tuple[SafeText, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def aware_audit_time(self) -> AccessibilityAuditArtifact:
        if self.audited_at.tzinfo is None or self.audited_at.utcoffset() is None:
            raise ValueError("accessibility audit time must be timezone-aware")
        return self


class ProviderTaskOutcome(RunnerContract):
    participant_id: SafeToken
    task_id: ProviderTaskId
    completed: bool
    duration_seconds: int = Field(ge=0, le=86_400)
    error_count: int = Field(ge=0, le=1_000)
    coaching_required: bool
    believed_data_uploaded: bool


class FiveProviderStudyArtifact(RunnerContract):
    schema_version: Literal["traceback.five-provider-study-artifact.v1"] = (
        "traceback.five-provider-study-artifact.v1"
    )
    conducted_at: datetime
    protocol_sha256: Sha256
    browser_capture_artifact_sha256: Sha256
    outcomes: tuple[ProviderTaskOutcome, ...] = Field(min_length=25, max_length=25)

    @model_validator(mode="after")
    def five_distinct_participants(self) -> FiveProviderStudyArtifact:
        if self.conducted_at.tzinfo is None or self.conducted_at.utcoffset() is None:
            raise ValueError("provider study time must be timezone-aware")
        participants = sorted({item.participant_id for item in self.outcomes})
        if len(participants) != 5:
            raise ValueError("provider study requires exactly five participants")
        keys = [(item.participant_id, item.task_id) for item in self.outcomes]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("provider task outcomes must be uniquely sorted")
        if set(keys) != {
            (participant, task)
            for participant in participants
            for task in ProviderTaskId
        }:
            raise ValueError("provider study must contain the complete frozen task matrix")
        by_task = {
            task: tuple(item for item in self.outcomes if item.task_id == task)
            for task in ProviderTaskId
        }
        identify = by_task[ProviderTaskId.IDENTIFY_JOB_STATE_OWNER_ACTION]
        if not all(item.completed and item.duration_seconds <= 10 for item in identify):
            raise ValueError("all five participants must identify state within ten seconds")
        recovery = (
            *by_task[ProviderTaskId.RECOVER_LOW_DISK],
            *by_task[ProviderTaskId.RECOVER_INTERRUPTION],
        )
        recovered_by_participant = {
            participant: all(
                item.completed and not item.coaching_required
                for item in recovery
                if item.participant_id == participant
            )
            for participant in participants
        }
        if sum(recovered_by_participant.values()) < 4:
            raise ValueError(
                "at least four participants must recover both seeded failures without coaching"
            )
        saved = by_task[ProviderTaskId.SAVE_VERIFY_WITHOUT_UPLOAD_BELIEF]
        if not all(
            item.completed and not item.believed_data_uploaded for item in saved
        ):
            raise ValueError(
                "all five participants must save and verify without upload belief"
            )
        installed = by_task[ProviderTaskId.DOCTOR_DEMO_VERIFY]
        if not all(item.completed and item.duration_seconds <= 300 for item in installed):
            raise ValueError(
                "all five participants must complete doctor demo verify within five minutes"
            )
        return self


class GateEvidence(RunnerContract):
    gate_id: GateId
    status: EvidenceStatus
    detail: SafeText
    evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def external_pass_requires_evidence(self) -> GateEvidence:
        if (
            self.gate_id
            in {
                GateId.APPROVED_HOST,
                GateId.FIVE_PROVIDER_STUDY,
                GateId.ACCESSIBILITY,
                GateId.SCREENSHOTS,
            }
            and self.status == EvidenceStatus.OBSERVED_PASS
            and self.evidence_sha256 is None
        ):
            raise ValueError("external gate pass requires content-addressed evidence")
        if self.status == EvidenceStatus.OBSERVED_PASS and self.evidence_sha256 is None:
            raise ValueError("observed pass requires content-addressed evidence")
        return self


class VerifiedExternalEvidence(RunnerContract):
    """Evidence authenticated outside the report by an independent verifier."""

    schema_version: Literal["traceback.verified-external-gate.v1"] = (
        "traceback.verified-external-gate.v1"
    )
    gate_id: GateId
    artifact_sha256: Sha256
    authority_head_sha256: Sha256
    authority_key_id: SafeToken
    verification_method: Literal["independent-signed-artifact"]
    verified_at: datetime
    expires_at: datetime
    approved_host_reference: SafeToken | None = None
    representative_users: int | None = Field(default=None, ge=0, le=100)
    keyboard_audit_passed: bool | None = None
    screen_reader_audit_passed: bool | None = None
    zoom_200_audit_passed: bool | None = None
    reviewed_browser_captures: bool | None = None
    measured_run_id: SafeToken | None = None
    host_run_sha256: Sha256 | None = None
    filter_performance_sha256: Sha256 | None = None
    initial_render_sha256: Sha256 | None = None
    stress_memory_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def exact_external_requirement(self) -> VerifiedExternalEvidence:
        for value in (self.verified_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("external evidence times must be timezone-aware")
        if self.expires_at <= self.verified_at:
            raise ValueError("external evidence must expire after verification")
        if self.gate_id == GateId.APPROVED_HOST:
            required = (
                self.approved_host_reference,
                self.measured_run_id,
                self.host_run_sha256,
                self.filter_performance_sha256,
                self.initial_render_sha256,
                self.stress_memory_sha256,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "approved-host evidence must bind the host and exact measured run"
                )
        elif self.gate_id == GateId.FIVE_PROVIDER_STUDY:
            if self.representative_users is None or self.representative_users < 5:
                raise ValueError(
                    "provider evidence requires at least five representative users"
                )
        elif self.gate_id == GateId.ACCESSIBILITY:
            if not (
                self.keyboard_audit_passed
                and self.screen_reader_audit_passed
                and self.zoom_200_audit_passed
            ):
                raise ValueError(
                    "accessibility evidence requires keyboard, screen-reader, and 200 percent zoom audits"
                )
        elif self.gate_id == GateId.SCREENSHOTS:
            if self.reviewed_browser_captures is not True:
                raise ValueError(
                    "screenshot evidence requires reviewed browser captures"
                )
        else:
            raise ValueError(
                "only external gates accept independently verified evidence"
            )
        return self


class SignedExternalEvidence(RunnerContract):
    schema_version: Literal["traceback.signed-external-gate.v1"] = (
        "traceback.signed-external-gate.v1"
    )
    evidence: VerifiedExternalEvidence
    signature: SignatureEnvelope


class PinnedAuthorityHead(RunnerContract):
    gate_id: GateId
    authority_key_id: SafeToken
    authority_head_sha256: Sha256

    @model_validator(mode="after")
    def external_gate_only(self) -> PinnedAuthorityHead:
        if self.gate_id not in {
            GateId.ACCESSIBILITY,
            GateId.APPROVED_HOST,
            GateId.FIVE_PROVIDER_STUDY,
            GateId.SCREENSHOTS,
        }:
            raise ValueError("authority heads may pin only external gates")
        return self


class ReleaseGateAuthorityPolicy(RunnerContract):
    schema_version: Literal["traceback.release-gate-authority-policy.v1"] = (
        "traceback.release-gate-authority-policy.v1"
    )
    policy_id: SafeToken
    issued_at: datetime
    expires_at: datetime
    pinned_heads: tuple[PinnedAuthorityHead, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def complete_pinned_authority(self) -> ReleaseGateAuthorityPolicy:
        for value in (self.issued_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("authority policy times must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("authority policy must expire after issuance")
        keys = tuple(
            (item.gate_id.value, item.authority_key_id) for item in self.pinned_heads
        )
        if (
            keys != tuple(sorted(keys))
            or len({item.gate_id for item in self.pinned_heads}) != 4
        ):
            raise ValueError("authority policy must uniquely pin every external gate")
        return self


class ReleaseGateDecision(RunnerContract):
    schema_version: Literal["traceback.release-gate-decision.v1"] = (
        "traceback.release-gate-decision.v1"
    )
    report_sha256: Sha256
    authority_policy_sha256: Sha256 | None = None
    trusted_external_evidence_sha256: tuple[Sha256, ...] = Field(max_length=4)
    capability_enabled: bool
    unmet_gates: tuple[GateId, ...] = Field(max_length=len(GateId))

    @model_validator(mode="after")
    def decision_is_derived(self) -> ReleaseGateDecision:
        if self.trusted_external_evidence_sha256 != tuple(
            sorted(set(self.trusted_external_evidence_sha256))
        ):
            raise ValueError(
                "trusted external evidence digests must be unique and sorted"
            )
        if self.unmet_gates != tuple(sorted(set(self.unmet_gates), key=str)):
            raise ValueError("unmet gates must be unique and sorted")
        if self.capability_enabled != (not self.unmet_gates):
            raise ValueError("capability state must be derived from unmet gates")
        if self.capability_enabled and len(self.trusted_external_evidence_sha256) != 4:
            raise ValueError(
                "enabled capability requires all four trusted external artifacts"
            )
        if self.capability_enabled and self.authority_policy_sha256 is None:
            raise ValueError("enabled capability requires a pinned authority policy")
        return self


class ProductGateReport(RunnerContract):
    schema_version: Literal["traceback.product-gate-report.v2"] = (
        "traceback.product-gate-report.v2"
    )
    host_run: HostRunEvidence
    filter_performance: PerformanceMeasurement
    initial_render: PerformanceMeasurement
    stress_memory: MemoryMeasurement
    screenshot_manifest_sha256: Sha256
    local_service_evidence: LocalServiceEvidence
    network_denial_evidence: NetworkDenialEvidence
    privacy_sentinel_evidence: PrivacySentinelEvidence
    external_requirements: tuple[ExternalRequirementEvidence, ...] = Field(
        min_length=6, max_length=6
    )
    release_control_evidence: ReleaseControlEvidence
    gate_evidence: tuple[GateEvidence, ...]
    capability_enabled: Literal[False] = False

    @model_validator(mode="after")
    def release_gate_is_derived(self) -> ProductGateReport:
        ids = [item.gate_id for item in self.gate_evidence]
        if ids != sorted(ids, key=str) or len(ids) != len(set(ids)):
            raise ValueError("gate evidence must be complete and uniquely sorted")
        if set(ids) != set(GateId):
            raise ValueError("gate report must include every E14 gate")
        by_id = {item.gate_id: item for item in self.gate_evidence}
        requirement_ids = [item.requirement_id for item in self.external_requirements]
        if (
            requirement_ids != sorted(requirement_ids, key=str)
            or len(set(requirement_ids)) != len(requirement_ids)
            or set(requirement_ids) != set(ExternalRequirementId)
        ):
            raise ValueError(
                "external requirement evidence must be complete and uniquely sorted"
            )
        requirements = {
            item.requirement_id: item for item in self.external_requirements
        }
        external_gate_requirements = {
            GateId.APPROVED_HOST: (ExternalRequirementId.APPROVED_HOST,),
            GateId.ACCESSIBILITY: (
                ExternalRequirementId.KEYBOARD_ONLY,
                ExternalRequirementId.SCREEN_READER,
                ExternalRequirementId.ZOOM_200,
            ),
            GateId.SCREENSHOTS: (ExternalRequirementId.REVIEWED_SCREENSHOTS,),
            GateId.FIVE_PROVIDER_STUDY: (
                ExternalRequirementId.FIVE_PROVIDER_TASKS,
            ),
        }
        for gate_id, required_ids in external_gate_requirements.items():
            states = tuple(requirements[item].state for item in required_ids)
            expected = (
                EvidenceStatus.REQUIRED_EXTERNAL
                if ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE in states
                else EvidenceStatus.OBSERVED_FAIL
                if ExternalRequirementState.OBSERVED_FAIL in states
                else EvidenceStatus.OBSERVED_PASS
            )
            gate = by_id[gate_id]
            if gate.status != expected:
                raise ValueError(
                    "external gate status must derive from exact requirement states"
                )
            if expected == EvidenceStatus.REQUIRED_EXTERNAL and (
                gate.evidence_sha256 is not None
            ):
                raise ValueError("unmet external gate cannot carry evidence")
        if any(
            item.state != ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE
            for item in self.external_requirements
        ):
            raise ValueError(
                "the local foundation report cannot claim observed external evidence"
            )
        if self.host_run.approved_host_reference is not None:
            raise ValueError(
                "the local foundation report cannot self-assert approved-host status"
            )
        measured = {
            GateId.FILTER_PERFORMANCE: self.filter_performance,
            GateId.INITIAL_RENDER: self.initial_render,
            GateId.STRESS_MEMORY: self.stress_memory,
        }
        for gate_id, measurement in measured.items():
            evidence = by_id[gate_id]
            if evidence.evidence_sha256 != sha256_bytes(
                canonical_json_bytes(measurement)
            ):
                raise ValueError(
                    "measurement evidence digest must bind the exact measurement"
                )
            if (
                evidence.status == EvidenceStatus.OBSERVED_PASS
                and not measurement.target_met
            ):
                raise ValueError("performance gate cannot pass a failed measurement")
        registered_output = canonical_json_bytes(
            {
                "host_run": self.host_run.model_dump(mode="json"),
                "filter": self.filter_performance.model_dump(mode="json"),
                "render": self.initial_render.model_dump(mode="json"),
                "memory": self.stress_memory.model_dump(mode="json"),
                "screenshot_manifest_sha256": self.screenshot_manifest_sha256,
                "local_service_sha256": sha256_bytes(
                    canonical_json_bytes(self.local_service_evidence)
                ),
            }
        )
        expected_privacy = _privacy_sentinel_probe(
            tuple(REGISTERED_PRIVACY_SENTINELS.items()),
            run_id=self.host_run.run_id,
            host_run_sha256=sha256_bytes(canonical_json_bytes(self.host_run)),
            output_payload_sha256=sha256_bytes(registered_output),
            serialized_output_clean=_privacy_clean(
                registered_output, tuple(REGISTERED_PRIVACY_SENTINELS.values())
            ),
            local_service_observations=(
                self.privacy_sentinel_evidence.local_service_observations
            ),
        )
        if self.privacy_sentinel_evidence != expected_privacy:
            raise ValueError(
                "privacy evidence must replay the registered probes and output"
            )
        if tuple(
            sorted(
                item.response_sha256
                for item in self.privacy_sentinel_evidence.local_service_observations
            )
        ) != self.local_service_evidence.privacy_rejection_response_sha256:
            raise ValueError(
                "privacy evidence must bind the exact local-service rejection responses"
            )
        exact_gate_evidence = {
            GateId.LOCAL_SERVICE_DETERMINISM: self.local_service_evidence,
            GateId.NO_EXTERNAL_NETWORK: self.network_denial_evidence,
            GateId.PRIVACY_SENTINELS: self.privacy_sentinel_evidence,
        }
        for gate_id, evidence_record in exact_gate_evidence.items():
            if by_id[gate_id].evidence_sha256 != sha256_bytes(
                canonical_json_bytes(evidence_record)
            ):
                raise ValueError(
                    "gate evidence digest must bind its exact observations"
                )
        if (
            by_id[GateId.LOCAL_SERVICE_DETERMINISM].status
            != EvidenceStatus.OBSERVED_PASS
        ):
            raise ValueError("deterministic local service must be an observed local pass")
        network_pass = all(
            item.denial_observed and item.guard_recorded
            for item in self.network_denial_evidence.observations
        ) and tuple(
            item.operation for item in self.network_denial_evidence.intercepted_attempts
        ) == tuple(operation.value for operation in NetworkProbeOperation)
        expected_network_status = (
            EvidenceStatus.OBSERVED_PASS
            if network_pass
            else EvidenceStatus.OBSERVED_FAIL
        )
        if by_id[GateId.NO_EXTERNAL_NETWORK].status != expected_network_status:
            raise ValueError("network gate status must be derived from exact probes")
        privacy_pass = self.privacy_sentinel_evidence.serialized_output_clean and all(
            item.rejected for item in self.privacy_sentinel_evidence.observations
        )
        expected_privacy_status = (
            EvidenceStatus.OBSERVED_PASS
            if privacy_pass
            else EvidenceStatus.OBSERVED_FAIL
        )
        if by_id[GateId.PRIVACY_SENTINELS].status != expected_privacy_status:
            raise ValueError("privacy gate status must be derived from exact probes")
        if self.network_denial_evidence.run_id != self.host_run.run_id:
            raise ValueError("network evidence must bind this host run")
        if self.privacy_sentinel_evidence.run_id != self.host_run.run_id:
            raise ValueError("privacy evidence must bind this host run")
        host_sha256 = sha256_bytes(canonical_json_bytes(self.host_run))
        if {
            self.network_denial_evidence.host_run_sha256,
            self.privacy_sentinel_evidence.host_run_sha256,
        } != {host_sha256}:
            raise ValueError(
                "privacy and network evidence must bind the exact host run"
            )
        local_service_sha256 = sha256_bytes(
            canonical_json_bytes(self.local_service_evidence)
        )
        if self.network_denial_evidence.local_service_sha256 != local_service_sha256:
            raise ValueError("network evidence must bind the local service journey")
        external_sha256 = sha256_bytes(
            canonical_json_bytes(
                [item.model_dump(mode="json") for item in self.external_requirements]
            )
        )
        if self.release_control_evidence != ReleaseControlEvidence(
            local_service_sha256=local_service_sha256,
            external_requirements_sha256=external_sha256,
        ):
            raise ValueError(
                "release controls must bind missing E12 and external evidence"
            )
        if by_id[GateId.APPROVED_HOST].status != EvidenceStatus.REQUIRED_EXTERNAL:
            raise ValueError("approved-host evidence remains externally required")
        return self


def _synthetic_catalog_ref(index: int) -> CatalogResultRef:
    """Create an E04-shaped fixture row; this is not a verified import."""

    bundle_sha256 = f"{index + 1:064x}"
    method_definition_sha256 = "c" * 64
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": bundle_sha256,
                "method_definition_sha256": method_definition_sha256,
            }
        )
    ).hexdigest()
    return CatalogResultRef(
        result_id=f"result_{identity[:40]}",
        bundle_sha256=bundle_sha256,
        bundle_record_id=f"record-{index:024x}",
        bundle_manifest_sha256=f"{index + 2:064x}",
        workflow_release_id="synthetic-workflow.v1",
        method_ref={
            "method_id": "mth_fragment_raw_query_length",
            "version": "1.0.0",
        },
        method_definition_sha256=method_definition_sha256,
        registry_sha256="d" * 64,
        registry_version=1,
        authority_head_sha256="e" * 64,
        authority_revision=2,
        authority_scope="scope_provider_west",
        capability_as_of=datetime(2026, 2, 1, tzinfo=UTC),
        qualification_state=CatalogQualificationState.QUALIFIED,
        display_role="provider_primary",
        research_inspectable=True,
        current_provider_eligible=True,
    )


@contextmanager
def _fixture_catalog(count: int) -> Iterator[ResultCatalog]:
    """Populate private-SQL fixture rows; never report these as verified imports."""

    with tempfile.TemporaryDirectory(prefix="traceback-e14-catalog-") as directory:
        root = Path(directory)
        imports = root / "imports"
        imports.mkdir()
        catalog = ResultCatalog(
            root / "catalog",
            import_roots={"root_synthetic": imports},
            trust_store=TrustStore(),
        )
        try:
            with catalog._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for start in range(0, count, 1_000):
                    rows = []
                    for index in range(start, min(start + 1_000, count)):
                        ref = _synthetic_catalog_ref(index)
                        rows.append(
                            (
                                ref.result_id,
                                ref.bundle_sha256,
                                ref.bundle_record_id,
                                ref.method_ref.method_id,
                                ref.method_ref.version,
                                ref.execution_state.value,
                                ref.information_state.value,
                                ref.trust_state.value,
                                ref.qualification_state.value,
                                ref.model_dump_json().encode(),
                            )
                        )
                    connection.executemany(
                        "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)", rows
                    )
                connection.commit()
            yield catalog
        finally:
            catalog.close()


def _nearest_rank_p95(samples: Sequence[int]) -> int:
    ordered = sorted(samples)
    rank = max(0, ((95 * len(ordered) + 99) // 100) - 1)
    return ordered[rank]


def _explorer_query(catalog: ResultCatalog, query: CatalogQuery) -> object:
    """Exercise the same E04-to-browser projection as the loopback API."""

    from traceback_runner.web.explorer import (
        CanonicalExplorerArtifactRepository,
        CatalogAuthorityIndex,
        IntegratedExplorerSource,
    )

    return IntegratedExplorerSource(
        catalog=catalog,
        authority=CatalogAuthorityIndex(()),
        artifacts=CanonicalExplorerArtifactRepository(()),
    ).query(query)


def _measure_filter(
    catalog: ResultCatalog, record_count: int
) -> PerformanceMeasurement:
    samples: list[int] = []
    query = CatalogQuery(
        method_refs=(
            {"method_id": "mth_fragment_raw_query_length", "version": "1.0.0"},
        ),
        qualification_states=(CatalogQualificationState.QUALIFIED,),
        limit=100,
    )
    for _ in range(20):
        started = time.perf_counter_ns()
        page = _explorer_query(catalog, query)
        samples.append((time.perf_counter_ns() - started) // 1_000)
        if len(page.results) != 100:
            raise ValueError("catalog performance query returned an incomplete page")
    p95 = _nearest_rank_p95(samples)
    return PerformanceMeasurement(
        name="filter_sort",
        record_count=record_count,
        samples_us=tuple(samples),
        p95_us=p95,
        target_us=FILTER_P95_TARGET_US,
        target_met=p95 <= FILTER_P95_TARGET_US,
    )


def _measure_initial_render(
    catalog: ResultCatalog, record_count: int
) -> PerformanceMeasurement:
    samples: list[int] = []
    query = CatalogQuery(limit=100)
    for _ in range(10):
        started = time.perf_counter_ns()
        page = _explorer_query(catalog, query)
        _catalog_page_bytes(page)
        samples.append((time.perf_counter_ns() - started) // 1_000)
    p95 = _nearest_rank_p95(samples)
    return PerformanceMeasurement(
        name="initial_render",
        record_count=record_count,
        samples_us=tuple(samples),
        p95_us=p95,
        target_us=INITIAL_RENDER_TARGET_US,
        target_met=p95 <= INITIAL_RENDER_TARGET_US,
    )


def _catalog_page_bytes(page: object) -> bytes:
    """Serialize the actual bounded E04 API payload used by the renderer."""

    return canonical_json_bytes(page)


@contextmanager
def deny_external_network(
    *, allowed_loopback_ports: Sequence[int] = ()
) -> Iterator[list[tuple[str, str]]]:
    """Deny egress while optionally preserving exact loopback service ports."""

    attempts: list[tuple[str, str]] = []
    method_names = (
        "connect",
        "connect_ex",
        "send",
        "sendall",
        "sendfile",
        "sendto",
        "sendmsg",
    )
    original_methods = {
        name: getattr(socket.socket, name)
        for name in method_names
        if hasattr(socket.socket, name)
    }
    original_create_connection = socket.create_connection
    resolver_names = (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    )
    original_resolvers = {name: getattr(socket, name) for name in resolver_names}
    allowed_ports = frozenset(allowed_loopback_ports)

    def allowed_address(address: object) -> bool:
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        try:
            return (
                int(address[1]) in allowed_ports
                and ipaddress.ip_address(str(address[0])).is_loopback
            )
        except (TypeError, ValueError):
            return False

    def allowed_connected_socket(instance: socket.socket) -> bool:
        try:
            return allowed_address(instance.getpeername()) or allowed_address(
                instance.getsockname()
            )
        except OSError:
            return False

    def blocked_socket_operation(name: str):
        original = original_methods[name]

        def deny(instance: socket.socket, *args: object, **kwargs: object) -> object:
            address = args[-1] if args else "connected-socket"
            if name in {"connect", "connect_ex"} and allowed_address(address):
                return original(instance, *args, **kwargs)
            if name == "sendto" and allowed_address(address):
                return original(instance, *args, **kwargs)
            if name not in {"connect", "connect_ex", "sendto"} and (
                allowed_connected_socket(instance)
            ):
                return original(instance, *args, **kwargs)
            attempts.append((name, repr(address)))
            raise RuntimeError("external network disabled by E14 harness")

        return deny

    def blocked_create_connection(*args: object, **kwargs: object) -> object:
        if args and allowed_address(args[0]):
            return original_create_connection(*args, **kwargs)
        attempts.append(("create_connection", repr(args[0] if args else "unknown")))
        raise RuntimeError("external network disabled by E14 harness")

    def blocked_getaddrinfo(*args: object, **kwargs: object) -> object:
        if len(args) >= 2 and allowed_address((args[0], args[1])):
            return original_resolvers["getaddrinfo"](*args, **kwargs)
        attempts.append(("resolver", repr(args[0] if args else "unknown")))
        raise RuntimeError("external network disabled by E14 harness")

    with _NETWORK_GUARD_LOCK:
        for name in original_methods:
            setattr(socket.socket, name, blocked_socket_operation(name))
        socket.create_connection = blocked_create_connection
        for name in original_resolvers:
            setattr(socket, name, blocked_getaddrinfo)
        try:
            yield attempts
        finally:
            for name, original in original_methods.items():
                setattr(socket.socket, name, original)
            socket.create_connection = original_create_connection
            for name, original in original_resolvers.items():
                setattr(socket, name, original)


def _local_http_request(
    service: RunningLocalWebService,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Make one bounded literal-loopback request without DNS or HTTP helpers."""

    body = canonical_json_bytes(payload) if payload is not None else b""
    request_headers = {
        "Connection": "close",
        "Host": service.config.authority,
        **(headers or {}),
    }
    if body:
        request_headers["Content-Length"] = str(len(body))
        request_headers["Content-Type"] = "application/json"
    request = (
        f"{method} {path} HTTP/1.1\r\n".encode("ascii")
        + b"".join(
            f"{name}: {value}\r\n".encode("ascii")
            for name, value in request_headers.items()
        )
        + b"\r\n"
        + body
    )
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(3)
    received = bytearray()
    try:
        client.connect((service.config.bind_host, service.config.port))
        client.sendall(request)
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > MAX_LOCAL_HTTP_RESPONSE_BYTES:
                raise ValueError("local service response exceeded the E14 byte bound")
    finally:
        client.close()
    head, separator, response_body = bytes(received).partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("local service returned a malformed HTTP response")
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split(b" ", 2)[1])
        response_headers = {
            name.decode("ascii").casefold(): value.decode("ascii").strip()
            for line in lines[1:]
            for name, value in (line.split(b":", 1),)
        }
    except (IndexError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("local service returned malformed HTTP metadata") from exc
    return status, response_headers, response_body


@contextmanager
def _fixture_local_service(
    catalog: ResultCatalog,
) -> Iterator[RunningLocalWebService]:
    from traceback_runner.web.explorer import (
        CanonicalExplorerArtifactRepository,
        CatalogAuthorityIndex,
        IntegratedExplorerSource,
    )

    with tempfile.TemporaryDirectory(prefix="traceback-e14-service-") as directory:
        root = Path(directory)
        store = JobStore(root / "jobs.sqlite3")
        explorer = IntegratedExplorerSource(
            catalog=catalog,
            authority=CatalogAuthorityIndex(()),
            artifacts=CanonicalExplorerArtifactRepository(()),
        )
        with RunningLocalWebService.start(
            store=store,
            state_directory=root / "state",
            explorer=explorer,
        ) as service:
            yield service


def _run_local_service_journey(
    service: RunningLocalWebService,
    sentinels: Sequence[tuple[PrivacySentinelClass, bytes]],
) -> tuple[LocalServiceEvidence, tuple[ServicePrivacyObservation, ...]]:
    request_path = "/api/v1/explorer/catalog?limit=100"
    unauthorized, _, unauthorized_body = _local_http_request(
        service, "GET", request_path
    )
    if unauthorized != 401 or not _privacy_clean(
        unauthorized_body, tuple(value for _, value in sentinels)
    ):
        raise ValueError("unauthorized local catalog probe did not fail closed")
    bootstrap_status, bootstrap_headers, bootstrap_body = _local_http_request(
        service,
        "POST",
        "/api/v1/session/bootstrap",
        headers={"Origin": service.base_url},
        payload={"bootstrap": service.bootstrap_code},
    )
    if bootstrap_status != 200:
        raise ValueError("local service bootstrap failed")
    bootstrap_payload = json.loads(bootstrap_body)
    if set(bootstrap_payload) != {"csrf_token"}:
        raise ValueError("local service bootstrap response changed")
    try:
        cookie = bootstrap_headers["set-cookie"].split(";", 1)[0]
    except KeyError as exc:
        raise ValueError("local service bootstrap omitted its session cookie") from exc
    authorized_headers = {"Cookie": cookie}
    first_status, _, first_body = _local_http_request(
        service,
        "GET",
        request_path,
        headers=authorized_headers,
    )
    second_status, _, second_body = _local_http_request(
        service,
        "GET",
        request_path,
        headers=authorized_headers,
    )
    first_payload = json.loads(first_body)
    if (
        first_status != 200
        or second_status != 200
        or first_body != second_body
        or canonical_json_bytes(first_payload) != first_body.rstrip(b"\n")
        or len(first_payload.get("results", ())) != 100
    ):
        raise ValueError("local catalog projection was not deterministic")
    eligibilities = tuple(
        item.get("eligibility", {}) for item in first_payload["results"]
    )
    if any(
        item.get("release_explorer_allowed") is not False
        or item.get("release_export_allowed") is not False
        or item.get("release_state") != "disabled_no_installed_authority"
        for item in eligibilities
    ):
        raise ValueError("local service exposed a release capability")

    asset_digests: list[tuple[str, str]] = []
    for path in ("/", "/assets/app.js", "/assets/styles.css"):
        status, _, content = _local_http_request(service, "GET", path)
        if status != 200:
            raise ValueError("packaged local asset was unavailable")
        lowered = content.lower()
        if b"http://" in lowered or b"https://" in lowered or b"//cdn" in lowered:
            raise ValueError("packaged local asset referenced external content")
        asset_digests.append((path, sha256_bytes(content)))

    privacy_observations: list[ServicePrivacyObservation] = []
    for sentinel_class, sentinel in sentinels:
        encoded = quote(sentinel.decode("ascii"), safe="")
        status, _, content = _local_http_request(
            service,
            "GET",
            (
                "/api/v1/explorer/catalog?limit=1&method_id="
                f"{encoded}&method_version=1.0.0"
            ),
            headers=authorized_headers,
        )
        if status != 400 or not _privacy_clean(content, (sentinel,)):
            raise ValueError("local service privacy probe did not reject safely")
        privacy_observations.append(
            ServicePrivacyObservation(
                sentinel_class=sentinel_class,
                sentinel_sha256=sha256_bytes(sentinel),
                status_code=status,
                response_sha256=sha256_bytes(content),
            )
        )

    from traceback_runner.web.explorer import ExplorerArtifactRecord

    e12_state = ExplorerArtifactRecord.model_fields["longitudinal_state"].default
    return (
        LocalServiceEvidence(
            unauthorized_status=unauthorized,
            bootstrap_status=bootstrap_status,
            authenticated_status=first_status,
            repeated_status=second_status,
            result_count=len(first_payload["results"]),
            response_sha256=sha256_bytes(first_body),
            repeated_response_sha256=sha256_bytes(second_body),
            packaged_assets_sha256=sha256_bytes(
                canonical_json_bytes(tuple(asset_digests))
            ),
            privacy_rejection_response_sha256=tuple(
                sorted(item.response_sha256 for item in privacy_observations)
            ),
            e12_state=e12_state,
        ),
        tuple(sorted(privacy_observations, key=lambda item: str(item.sentinel_class))),
    )


def load_screenshot_manifest(path: Path) -> tuple[ScreenshotManifest, bytes]:
    content = path.read_bytes().removesuffix(b"\n")
    parsed = ScreenshotManifest.model_validate_json(content)
    if canonical_json_bytes(parsed) != content:
        raise ValueError("screenshot manifest must use canonical JSON")
    return parsed, content


def _privacy_clean(payload: bytes, sentinels: Sequence[bytes]) -> bool:
    folded = payload.lower()
    return all(sentinel.lower() not in folded for sentinel in sentinels)


def _privacy_sentinel_probe(
    sentinels: Sequence[tuple[PrivacySentinelClass, bytes]],
    *,
    run_id: str,
    host_run_sha256: str,
    output_payload_sha256: str,
    serialized_output_clean: bool,
    local_service_observations: tuple[ServicePrivacyObservation, ...] | None = None,
) -> PrivacySentinelEvidence:
    """Inject every sentinel through catalog, problem, and screenshot contracts."""

    observations: list[PrivacyPathObservation] = []
    for sentinel_class, sentinel in sentinels:
        value = sentinel.decode("ascii")
        sentinel_sha256 = sha256_bytes(sentinel)
        poisoned_record = _synthetic_catalog_ref(0).model_dump(mode="json")
        poisoned_record["result_id"] = value
        try:
            CatalogResultRef.model_validate(poisoned_record)
        except ValidationError:
            catalog_rejected = True
        else:
            catalog_rejected = False
        observations.append(
            PrivacyPathObservation(
                sentinel_class=sentinel_class,
                sentinel_sha256=sentinel_sha256,
                path=PrivacyProbePath.CATALOG,
                rejected=catalog_rejected,
            )
        )

        try:
            ProblemDetail(
                code="TBX-OUT-001",
                problem=value,
                cause="Synthetic privacy probe",
                fix="Remove the forbidden value",
                docs_path="docs/operator/privacy.md",
                owner=ProblemOwner.SUPPORT,
                retryable=False,
                correlation_id="cor_0000000000000000",
                preserved_work="Verified work remains available",
                repeated_work="No work was repeated",
            )
        except ValidationError:
            problem_rejected = True
        else:
            problem_rejected = False
        observations.append(
            PrivacyPathObservation(
                sentinel_class=sentinel_class,
                sentinel_sha256=sentinel_sha256,
                path=PrivacyProbePath.PROBLEM_RESPONSE,
                rejected=problem_rejected,
            )
        )

        try:
            ScreenshotManifest(
                fixtures=(
                    AccessibilityFixture(
                        fixture_id="privacy-probe",
                        viewport_width_px=1280,
                        zoom_percent=200,
                        keyboard_order=("queue",),
                        screen_reader_names=(value,),
                        non_color_status_text=("Blocked",),
                    ),
                )
            )
        except ValidationError:
            screenshot_rejected = True
        else:
            screenshot_rejected = False
        observations.append(
            PrivacyPathObservation(
                sentinel_class=sentinel_class,
                sentinel_sha256=sentinel_sha256,
                path=PrivacyProbePath.SCREENSHOT,
                rejected=screenshot_rejected,
            )
        )
    return PrivacySentinelEvidence(
        run_id=run_id,
        host_run_sha256=host_run_sha256,
        output_payload_sha256=output_payload_sha256,
        serialized_output_clean=serialized_output_clean,
        local_service_observations=(
            local_service_observations
            if local_service_observations is not None
            else tuple(
                ServicePrivacyObservation(
                    sentinel_class=sentinel_class,
                    sentinel_sha256=sha256_bytes(sentinel),
                    status_code=400,
                    response_sha256=sha256_bytes(
                        b"synthetic-local-service-probe:" + sentinel
                    ),
                )
                for sentinel_class, sentinel in sorted(
                    sentinels, key=lambda item: str(item[0])
                )
            )
        ),
        observations=tuple(
            sorted(
                observations,
                key=lambda item: (item.sentinel_class.value, item.path.value),
            )
        ),
    )


_EXTERNAL_GATES = frozenset(
    {
        GateId.ACCESSIBILITY,
        GateId.APPROVED_HOST,
        GateId.FIVE_PROVIDER_STUDY,
        GateId.SCREENSHOTS,
    }
)


def derive_release_gate(
    report: ProductGateReport,
    *,
    signed_external_evidence: Sequence[SignedExternalEvidence] = (),
    trust_store: TrustStore | None = None,
    authority_policy: ReleaseGateAuthorityPolicy | None = None,
    now: datetime | None = None,
) -> ReleaseGateDecision:
    """Derive capability state from measurements and independently verified evidence."""

    # No authenticated installation boundary exists yet. Caller-provided trust
    # material and policy objects cannot establish independent release authority.
    verified: dict[GateId, VerifiedExternalEvidence] = {}
    accepted_envelopes: list[SignedExternalEvidence] = []
    current = now
    if current is not None and (current.tzinfo is None or current.utcoffset() is None):
        raise ValueError("release-gate time must be timezone-aware")
    if set(verified) - _EXTERNAL_GATES:
        raise ValueError("verified evidence contains a non-external gate")
    evidence = {item.gate_id: item for item in report.gate_evidence}
    unmet: set[GateId] = {
        item.gate_id
        for item in report.gate_evidence
        if item.status != EvidenceStatus.OBSERVED_PASS
    }
    if not report.filter_performance.target_met:
        unmet.add(GateId.FILTER_PERFORMANCE)
    if not report.initial_render.target_met:
        unmet.add(GateId.INITIAL_RENDER)
    if not report.stress_memory.target_met:
        unmet.add(GateId.STRESS_MEMORY)
    for gate_id in _EXTERNAL_GATES:
        trusted = verified.get(gate_id)
        claimed = evidence[gate_id]
        if (
            trusted is None
            or claimed.evidence_sha256 is None
            or trusted.artifact_sha256 != claimed.evidence_sha256
        ):
            unmet.add(gate_id)
    host = verified.get(GateId.APPROVED_HOST)
    if (
        host is None
        or host.approved_host_reference != report.host_run.approved_host_reference
        or host.measured_run_id != report.host_run.run_id
        or host.host_run_sha256 != sha256_bytes(canonical_json_bytes(report.host_run))
        or host.filter_performance_sha256
        != sha256_bytes(canonical_json_bytes(report.filter_performance))
        or host.initial_render_sha256
        != sha256_bytes(canonical_json_bytes(report.initial_render))
        or host.stress_memory_sha256
        != sha256_bytes(canonical_json_bytes(report.stress_memory))
    ):
        unmet.add(GateId.APPROVED_HOST)
    trusted_digests = tuple(
        sorted(sha256_bytes(canonical_json_bytes(item)) for item in accepted_envelopes)
    )
    return ReleaseGateDecision(
        report_sha256=sha256_bytes(canonical_json_bytes(report)),
        authority_policy_sha256=(
            sha256_bytes(canonical_json_bytes(authority_policy))
            if authority_policy is not None
            else None
        ),
        trusted_external_evidence_sha256=trusted_digests,
        capability_enabled=not unmet,
        unmet_gates=tuple(sorted(unmet, key=str)),
    )


def run_foundation_gates(
    *,
    screenshot_manifest_path: Path,
    run_id: str,
    captured_at: datetime | None = None,
) -> ProductGateReport:
    """Run synthetic/local gates without upgrading external evidence states."""

    manifest, manifest_bytes = load_screenshot_manifest(screenshot_manifest_path)
    del manifest
    sentinels = tuple(REGISTERED_PRIVACY_SENTINELS.items())
    host_run = HostRunEvidence(
        run_id=run_id,
        captured_at=captured_at or datetime.now(UTC),
        python_version=platform.python_version(),
        operating_system=platform.system() or "unknown-os",
        machine=platform.machine() or "unknown-machine",
        processor=platform.processor() or "unknown-processor",
        approved_host_reference=None,
    )
    host_run_sha256 = sha256_bytes(canonical_json_bytes(host_run))
    network_probe_results: list[tuple[NetworkProbeOperation, bool, bool]] = []
    probe_payload = REGISTERED_NETWORK_PAYLOAD

    with _fixture_catalog(CATALOG_RECORDS) as catalog:
        with _fixture_local_service(catalog) as service:
            with deny_external_network(
                allowed_loopback_ports=(service.config.port,)
            ) as network_attempts:
                filter_measurement = _measure_filter(catalog, CATALOG_RECORDS)
                render_measurement = _measure_initial_render(
                    catalog, CATALOG_RECORDS
                )
                local_service_evidence, service_privacy = (
                    _run_local_service_journey(service, sentinels)
                )

                tracemalloc.start()
                try:
                    with _fixture_catalog(STRESS_RECORDS) as stress_catalog:
                        stress_page = _explorer_query(
                            stress_catalog, CatalogQuery(limit=100)
                        )
                        _catalog_page_bytes(stress_page)
                    _, peak_bytes = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    for operation_name, operation in (
                        (
                            NetworkProbeOperation.CONNECT_EX,
                            lambda: probe_socket.connect_ex(
                                REGISTERED_NETWORK_TARGET
                            ),
                        ),
                        (
                            NetworkProbeOperation.SENDTO,
                            lambda: probe_socket.sendto(
                                probe_payload, REGISTERED_NETWORK_TARGET
                            ),
                        ),
                    ):
                        before = len(network_attempts)
                        try:
                            operation()
                        except RuntimeError:
                            denial_observed = True
                        else:
                            denial_observed = False
                        guard_recorded = any(
                            name == operation_name.value
                            for name, _ in network_attempts[before:]
                        )
                        network_probe_results.append(
                            (operation_name, denial_observed, guard_recorded)
                        )
                finally:
                    probe_socket.close()

    memory = MemoryMeasurement(
        peak_bytes=peak_bytes,
        target_met=peak_bytes <= MAX_SYNTHETIC_PEAK_BYTES,
    )
    network_denial_evidence = NetworkDenialEvidence(
        run_id=run_id,
        host_run_sha256=host_run_sha256,
        local_service_sha256=sha256_bytes(
            canonical_json_bytes(local_service_evidence)
        ),
        observations=tuple(
            NetworkProbeObservation(
                operation=operation,
                denial_observed=denied,
                guard_recorded=recorded,
                payload_sha256=(
                    sha256_bytes(probe_payload)
                    if operation == NetworkProbeOperation.SENDTO
                    else None
                ),
            )
            for operation, denied, recorded in network_probe_results
        ),
        intercepted_attempts=tuple(
            NetworkInterceptObservation(
                operation=name,
                target_sha256=sha256_bytes(target.encode("utf-8")),
            )
            for name, target in network_attempts
        ),
    )
    privacy_payload = canonical_json_bytes(
        {
            "host_run": host_run.model_dump(mode="json"),
            "filter": filter_measurement.model_dump(mode="json"),
            "render": render_measurement.model_dump(mode="json"),
            "memory": memory.model_dump(mode="json"),
            "screenshot_manifest_sha256": sha256_bytes(manifest_bytes),
            "local_service_sha256": sha256_bytes(
                canonical_json_bytes(local_service_evidence)
            ),
        }
    )
    serialized_output_clean = _privacy_clean(
        privacy_payload,
        tuple(sentinel for _, sentinel in sentinels),
    )
    privacy_sentinel_evidence = _privacy_sentinel_probe(
        sentinels,
        run_id=run_id,
        host_run_sha256=host_run_sha256,
        output_payload_sha256=sha256_bytes(privacy_payload),
        serialized_output_clean=serialized_output_clean,
        local_service_observations=service_privacy,
    )
    privacy_pass = serialized_output_clean and all(
        item.rejected for item in privacy_sentinel_evidence.observations
    )
    filter_sha256 = sha256_bytes(canonical_json_bytes(filter_measurement))
    render_sha256 = sha256_bytes(canonical_json_bytes(render_measurement))
    memory_sha256 = sha256_bytes(canonical_json_bytes(memory))
    network_sha256 = sha256_bytes(canonical_json_bytes(network_denial_evidence))
    privacy_sha256 = sha256_bytes(canonical_json_bytes(privacy_sentinel_evidence))
    local_service_sha256 = sha256_bytes(
        canonical_json_bytes(local_service_evidence)
    )
    external_requirements = tuple(
        sorted(
            (
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.APPROVED_HOST,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail=(
                        "No approved workstation profile or approved-host run was "
                        "supplied"
                    ),
                ),
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.KEYBOARD_ONLY,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail="Keyboard-only task audit has not been observed",
                ),
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.SCREEN_READER,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail="Screen-reader task audit has not been observed",
                ),
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.ZOOM_200,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail="200 percent zoom and reflow audit has not been observed",
                ),
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.REVIEWED_SCREENSHOTS,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail="Reviewed browser captures have not been supplied",
                ),
                ExternalRequirementEvidence(
                    requirement_id=ExternalRequirementId.FIVE_PROVIDER_TASKS,
                    state=ExternalRequirementState.UNMET_NO_OBSERVED_EVIDENCE,
                    detail="Five-provider frozen task matrix has not been observed",
                ),
            ),
            key=lambda item: str(item.requirement_id),
        )
    )
    release_control_evidence = ReleaseControlEvidence(
        local_service_sha256=local_service_sha256,
        external_requirements_sha256=sha256_bytes(
            canonical_json_bytes(
                [item.model_dump(mode="json") for item in external_requirements]
            )
        ),
    )

    evidence = (
        GateEvidence(
            gate_id=GateId.ACCESSIBILITY,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Keyboard, screen-reader, and 200 percent zoom audits remain unmet",
        ),
        GateEvidence(
            gate_id=GateId.APPROVED_HOST,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Current machine is recorded but has no approved-host reference",
        ),
        GateEvidence(
            gate_id=GateId.FILTER_PERFORMANCE,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail=(
                "Private-SQL timing over 10000 E04-shaped fixture rows only; "
                "10000 verified E04 imports are unavailable"
            ),
            evidence_sha256=filter_sha256,
        ),
        GateEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Five-provider task evidence has not been collected",
        ),
        GateEvidence(
            gate_id=GateId.INITIAL_RENDER,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail=(
                "Python projection serialization only; HTTP, JavaScript, layout, "
                "and DOM-ready browser timing are unavailable"
            ),
            evidence_sha256=render_sha256,
        ),
        GateEvidence(
            gate_id=GateId.LOCAL_SERVICE_DETERMINISM,
            status=EvidenceStatus.OBSERVED_PASS,
            detail=(
                "Packaged loopback catalog repeated byte-identically with release "
                "controls disabled"
            ),
            evidence_sha256=local_service_sha256,
        ),
        GateEvidence(
            gate_id=GateId.NO_EXTERNAL_NETWORK,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if all(
                    item.denial_observed and item.guard_recorded
                    for item in network_denial_evidence.observations
                )
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail=(
                "Socket connect_ex and sendto probes were denied during the complete "
                "harness run"
            ),
            evidence_sha256=network_sha256,
        ),
        GateEvidence(
            gate_id=GateId.PRIVACY_SENTINELS,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if privacy_pass
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail=(
                "Every forbidden class was rejected by catalog, problem, screenshot, "
                "and packaged HTTP paths"
            ),
            evidence_sha256=privacy_sha256,
        ),
        GateEvidence(
            gate_id=GateId.SCREENSHOTS,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Synthetic metadata is present but reviewed browser captures remain unmet",
        ),
        GateEvidence(
            gate_id=GateId.STRESS_MEMORY,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail=(
                "tracemalloc peak for 100000 private-SQL fixture rows only; process RSS, "
                "SQLite and native allocations, and browser memory are unavailable"
            ),
            evidence_sha256=memory_sha256,
        ),
    )
    return ProductGateReport(
        host_run=host_run,
        filter_performance=filter_measurement,
        initial_render=render_measurement,
        stress_memory=memory,
        screenshot_manifest_sha256=sha256_bytes(manifest_bytes),
        local_service_evidence=local_service_evidence,
        network_denial_evidence=network_denial_evidence,
        privacy_sentinel_evidence=privacy_sentinel_evidence,
        external_requirements=external_requirements,
        release_control_evidence=release_control_evidence,
        gate_evidence=tuple(sorted(evidence, key=lambda item: str(item.gate_id))),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        raise SystemExit(
            "usage: python -m traceback_runner.product_gates MANIFEST RUN_ID"
        )
    report = run_foundation_gates(
        screenshot_manifest_path=Path(args[0]),
        run_id=args[1],
    )
    sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
