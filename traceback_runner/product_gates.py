"""Offline E14 foundation harness with explicit unpassed external gates.

The harness measures synthetic catalog-shaped work on the current machine.  A
local measurement is not approved-host evidence, accessibility metadata is not
a manual accessibility audit, and fixtures are not a five-provider study.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import time
import tracemalloc
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from traceback_runner.contracts import RunnerContract
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.signing import (
    KeyPurpose,
    SignatureEnvelope,
    SigningError,
    TrustStore,
    verify_signature,
)
from traceback_runner.web.contracts import ProblemDetail, ProblemOwner, SafeText

CATALOG_RECORDS = 10_000
STRESS_RECORDS = 100_000
FILTER_P95_TARGET_US = 250_000
INITIAL_RENDER_TARGET_US = 2_000_000
MAX_SYNTHETIC_PEAK_BYTES = 256 * 1024 * 1024

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


class EvidenceStatus(StrEnum):
    OBSERVED_PASS = "observed_pass"
    OBSERVED_FAIL = "observed_fail"
    OBSERVED_LOCAL_UNAPPROVED = "observed_local_unapproved"
    FIXTURE_ONLY = "fixture_only"
    REQUIRED_EXTERNAL = "required_external"


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

    @model_validator(mode="after")
    def exact_external_requirement(self) -> VerifiedExternalEvidence:
        for value in (self.verified_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("external evidence times must be timezone-aware")
        if self.expires_at <= self.verified_at:
            raise ValueError("external evidence must expire after verification")
        if self.gate_id == GateId.APPROVED_HOST:
            if self.approved_host_reference is None:
                raise ValueError("approved-host evidence must bind the host reference")
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


class ReleaseGateDecision(RunnerContract):
    schema_version: Literal["traceback.release-gate-decision.v1"] = (
        "traceback.release-gate-decision.v1"
    )
    report_sha256: Sha256
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
        return self


class ProductGateReport(RunnerContract):
    schema_version: Literal["traceback.product-gate-report.v1"] = (
        "traceback.product-gate-report.v1"
    )
    host_run: HostRunEvidence
    filter_performance: PerformanceMeasurement
    initial_render: PerformanceMeasurement
    stress_memory: MemoryMeasurement
    screenshot_manifest_sha256: Sha256
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
        measured = {
            GateId.FILTER_PERFORMANCE: self.filter_performance.target_met,
            GateId.INITIAL_RENDER: self.initial_render.target_met,
            GateId.STRESS_MEMORY: self.stress_memory.target_met,
        }
        for gate_id, target_met in measured.items():
            if by_id[gate_id].status == EvidenceStatus.OBSERVED_PASS and not target_met:
                raise ValueError("performance gate cannot pass a failed measurement")
        if (by_id[GateId.APPROVED_HOST].status == EvidenceStatus.OBSERVED_PASS) != (
            self.host_run.approved_host_reference is not None
        ):
            raise ValueError("approved-host status must match the bound host reference")
        return self


@dataclass(frozen=True, slots=True)
class _SyntheticCatalogRecord:
    result_id: str
    method_id: str
    state: str
    created_order: int


def _synthetic_records(count: int) -> tuple[_SyntheticCatalogRecord, ...]:
    methods = ("fragment_span", "cell_origin", "cna_dosage", "cna_segmented")
    states = ("complete", "insufficient", "failed", "not_run")
    return tuple(
        _SyntheticCatalogRecord(
            result_id=f"result_{index:040x}",
            method_id=methods[index % len(methods)],
            state=states[(index // len(methods)) % len(states)],
            created_order=index,
        )
        for index in range(count)
    )


def _filter_sort(
    records: Sequence[_SyntheticCatalogRecord], *, method_id: str, state: str
) -> tuple[_SyntheticCatalogRecord, ...]:
    matches = (
        item for item in records if item.method_id == method_id and item.state == state
    )
    return tuple(
        sorted(matches, key=lambda item: (-item.created_order, item.result_id))
    )


def _nearest_rank_p95(samples: Sequence[int]) -> int:
    ordered = sorted(samples)
    rank = max(0, ((95 * len(ordered) + 99) // 100) - 1)
    return ordered[rank]


def _measure_filter(
    records: Sequence[_SyntheticCatalogRecord],
) -> PerformanceMeasurement:
    samples: list[int] = []
    for index in range(20):
        method = ("fragment_span", "cell_origin", "cna_dosage", "cna_segmented")[
            index % 4
        ]
        state = ("complete", "insufficient", "failed", "not_run")[(index // 4) % 4]
        started = time.perf_counter_ns()
        _filter_sort(records, method_id=method, state=state)
        samples.append((time.perf_counter_ns() - started) // 1_000)
    p95 = _nearest_rank_p95(samples)
    return PerformanceMeasurement(
        name="filter_sort",
        record_count=len(records),
        samples_us=tuple(samples),
        p95_us=p95,
        target_us=FILTER_P95_TARGET_US,
        target_met=p95 <= FILTER_P95_TARGET_US,
    )


def _measure_initial_render(
    records: Sequence[_SyntheticCatalogRecord],
) -> PerformanceMeasurement:
    samples: list[int] = []
    for _ in range(10):
        started = time.perf_counter_ns()
        page = sorted(records, key=lambda item: item.result_id)[:100]
        _catalog_page_bytes(page)
        samples.append((time.perf_counter_ns() - started) // 1_000)
    p95 = _nearest_rank_p95(samples)
    return PerformanceMeasurement(
        name="initial_render",
        record_count=len(records),
        samples_us=tuple(samples),
        p95_us=p95,
        target_us=INITIAL_RENDER_TARGET_US,
        target_met=p95 <= INITIAL_RENDER_TARGET_US,
    )


def _catalog_page_bytes(records: Sequence[_SyntheticCatalogRecord]) -> bytes:
    """Serialize the same bounded public fields measured by initial render."""

    return canonical_json_bytes(
        [
            {
                "result_id": _SAFE_TEXT.validate_python(item.result_id),
                "method_id": _SAFE_TEXT.validate_python(item.method_id),
                "state": _SAFE_TEXT.validate_python(item.state),
            }
            for item in records
        ]
    )


@contextmanager
def deny_external_network() -> Iterator[list[str]]:
    """Deny socket connections in this Python process during an offline run."""

    attempts: list[str] = []
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

    def blocked_socket_operation(
        instance: socket.socket, *args: object, **kwargs: object
    ) -> None:
        del instance, kwargs
        address = args[-1] if args else "connected-socket"
        attempts.append(repr(address))
        raise RuntimeError("external network disabled by E14 harness")

    def blocked_create_connection(*args: object, **kwargs: object) -> None:
        del kwargs
        attempts.append(repr(args[0] if args else "unknown"))
        raise RuntimeError("external network disabled by E14 harness")

    def blocked_getaddrinfo(*args: object, **kwargs: object) -> None:
        del kwargs
        attempts.append(repr(args[0] if args else "unknown"))
        raise RuntimeError("external network disabled by E14 harness")

    with _NETWORK_GUARD_LOCK:
        for name in original_methods:
            setattr(socket.socket, name, blocked_socket_operation)
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


def load_screenshot_manifest(path: Path) -> tuple[ScreenshotManifest, bytes]:
    content = path.read_bytes().removesuffix(b"\n")
    parsed = ScreenshotManifest.model_validate_json(content)
    if canonical_json_bytes(parsed) != content:
        raise ValueError("screenshot manifest must use canonical JSON")
    return parsed, content


def _privacy_clean(payload: bytes, sentinels: Sequence[bytes]) -> bool:
    folded = payload.lower()
    return all(sentinel.lower() not in folded for sentinel in sentinels)


def _privacy_sentinel_probe(sentinels: Sequence[bytes]) -> bool:
    """Inject every sentinel through catalog, problem, and screenshot contracts."""

    for sentinel in sentinels:
        value = sentinel.decode("ascii")
        rejected_paths = 0
        poisoned_record = _SyntheticCatalogRecord(
            result_id=value,
            method_id="fragment_span",
            state="complete",
            created_order=0,
        )
        try:
            _catalog_page_bytes((poisoned_record,))
        except ValidationError:
            rejected_paths += 1

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
            rejected_paths += 1

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
            rejected_paths += 1

        if rejected_paths != 3:
            return False
    return bool(sentinels)


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
    now: datetime | None = None,
) -> ReleaseGateDecision:
    """Derive capability state from measurements and independently verified evidence."""

    verified: dict[GateId, VerifiedExternalEvidence] = {}
    accepted_envelopes: list[SignedExternalEvidence] = []
    current = now
    if current is not None and (current.tzinfo is None or current.utcoffset() is None):
        raise ValueError("release-gate time must be timezone-aware")
    if trust_store is not None and current is not None:
        for envelope in signed_external_evidence:
            try:
                verify_signature(
                    canonical_json_bytes(envelope.evidence),
                    envelope.signature,
                    trust_store,
                    purpose=KeyPurpose.RELEASE,
                )
            except SigningError:
                continue
            if envelope.evidence.authority_key_id != envelope.signature.key_id:
                continue
            if (
                not envelope.evidence.verified_at
                <= current
                < envelope.evidence.expires_at
            ):
                continue
            if envelope.evidence.gate_id in verified:
                raise ValueError("signed external evidence must be unique by gate")
            verified[envelope.evidence.gate_id] = envelope.evidence
            accepted_envelopes.append(envelope)
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
    ):
        unmet.add(GateId.APPROVED_HOST)
    trusted_digests = tuple(
        sorted(sha256_bytes(canonical_json_bytes(item)) for item in accepted_envelopes)
    )
    return ReleaseGateDecision(
        report_sha256=sha256_bytes(canonical_json_bytes(report)),
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
    sentinels = (
        b"donor_id=private-0001",
        b"read_id=private-read-0001",
        b"/Users/private/raw-input.bam",
        b"ACGTACGTACGTACGTACGTACGTACGTACGT",
    )

    with deny_external_network() as network_attempts:
        records = _synthetic_records(CATALOG_RECORDS)
        filter_measurement = _measure_filter(records)
        render_measurement = _measure_initial_render(records)

        tracemalloc.start()
        stress_records = _synthetic_records(STRESS_RECORDS)
        _filter_sort(
            stress_records,
            method_id="fragment_span",
            state="complete",
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del stress_records
        probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for operation in (
                lambda: probe_socket.connect_ex(("127.0.0.1", 9)),
                lambda: probe_socket.sendto(b"privacy-safe-probe", ("127.0.0.1", 9)),
            ):
                try:
                    operation()
                except RuntimeError:
                    pass
        finally:
            probe_socket.close()

    memory = MemoryMeasurement(
        peak_bytes=peak_bytes,
        target_met=peak_bytes <= MAX_SYNTHETIC_PEAK_BYTES,
    )
    host_run = HostRunEvidence(
        run_id=run_id,
        captured_at=captured_at or datetime.now(UTC),
        python_version=platform.python_version(),
        operating_system=platform.system() or "unknown-os",
        machine=platform.machine() or "unknown-machine",
        processor=platform.processor() or "unknown-processor",
        approved_host_reference=None,
    )
    privacy_payload = canonical_json_bytes(
        {
            "host_run": host_run.model_dump(mode="json"),
            "filter": filter_measurement.model_dump(mode="json"),
            "render": render_measurement.model_dump(mode="json"),
            "memory": memory.model_dump(mode="json"),
            "screenshots": json.loads(manifest_bytes),
        }
    )
    privacy_pass = _privacy_sentinel_probe(sentinels) and _privacy_clean(
        privacy_payload, sentinels
    )
    filter_sha256 = sha256_bytes(canonical_json_bytes(filter_measurement))
    render_sha256 = sha256_bytes(canonical_json_bytes(render_measurement))
    memory_sha256 = sha256_bytes(canonical_json_bytes(memory))
    network_sha256 = sha256_bytes(
        canonical_json_bytes({"blocked_operations": len(network_attempts)})
    )
    privacy_sha256 = sha256_bytes(
        canonical_json_bytes(
            {"sentinel_classes": len(sentinels), "real_paths_per_sentinel": 3}
        )
    )

    evidence = (
        GateEvidence(
            gate_id=GateId.ACCESSIBILITY,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail="Keyboard, screen-reader, and 200 percent zoom metadata only; manual audit required",
        ),
        GateEvidence(
            gate_id=GateId.APPROVED_HOST,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Current machine is recorded but has no approved-host reference",
        ),
        GateEvidence(
            gate_id=GateId.FILTER_PERFORMANCE,
            status=(
                EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED
                if filter_measurement.target_met
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Measured locally against 10000 synthetic records; approved-host rerun required",
            evidence_sha256=filter_sha256,
        ),
        GateEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Five-provider task evidence has not been collected",
        ),
        GateEvidence(
            gate_id=GateId.INITIAL_RENDER,
            status=(
                EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED
                if render_measurement.target_met
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Measured local projection serialization; approved-host browser evidence required",
            evidence_sha256=render_sha256,
        ),
        GateEvidence(
            gate_id=GateId.NO_EXTERNAL_NETWORK,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if len(network_attempts) == 2
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Socket connect_ex and sendto probes were denied during the complete harness run",
            evidence_sha256=network_sha256,
        ),
        GateEvidence(
            gate_id=GateId.PRIVACY_SENTINELS,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if privacy_pass
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Every forbidden class was injected through catalog, response, and screenshot paths and rejected",
            evidence_sha256=privacy_sha256,
        ),
        GateEvidence(
            gate_id=GateId.SCREENSHOTS,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail="Synthetic screenshot state metadata only; reviewed browser captures required",
        ),
        GateEvidence(
            gate_id=GateId.STRESS_MEMORY,
            status=(
                EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED
                if memory.target_met
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Measured peak Python allocations for the 100000-record stress fixture",
            evidence_sha256=memory_sha256,
        ),
    )
    return ProductGateReport(
        host_run=host_run,
        filter_performance=filter_measurement,
        initial_render=render_measurement,
        stress_memory=memory,
        screenshot_manifest_sha256=sha256_bytes(manifest_bytes),
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
