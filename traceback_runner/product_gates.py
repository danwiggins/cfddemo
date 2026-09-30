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
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from traceback_runner.contracts import RunnerContract
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes

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
    python_version: str = Field(min_length=3, max_length=32)
    operating_system: str = Field(min_length=2, max_length=64)
    machine: str = Field(min_length=2, max_length=64)
    processor: str = Field(min_length=2, max_length=128)
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
    screen_reader_names: tuple[str, ...] = Field(min_length=1, max_length=64)
    non_color_status_text: tuple[str, ...] = Field(min_length=1, max_length=32)


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
    detail: str = Field(min_length=1, max_length=512)
    evidence_ref: SafeToken | None = None

    @model_validator(mode="after")
    def external_pass_requires_evidence(self) -> GateEvidence:
        if self.gate_id in {
            GateId.APPROVED_HOST,
            GateId.FIVE_PROVIDER_STUDY,
            GateId.ACCESSIBILITY,
            GateId.SCREENSHOTS,
        } and self.status == EvidenceStatus.OBSERVED_PASS:
            if self.evidence_ref is None:
                raise ValueError("external gate pass requires an evidence reference")
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
    capability_enabled: bool

    @model_validator(mode="after")
    def release_gate_is_derived(self) -> ProductGateReport:
        ids = [item.gate_id for item in self.gate_evidence]
        if ids != sorted(ids, key=str) or len(ids) != len(set(ids)):
            raise ValueError("gate evidence must be complete and uniquely sorted")
        if set(ids) != set(GateId):
            raise ValueError("gate report must include every E14 gate")
        all_pass = all(
            item.status == EvidenceStatus.OBSERVED_PASS
            for item in self.gate_evidence
        )
        if self.capability_enabled != all_pass:
            raise ValueError("capability state must be derived from all gate evidence")
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
        item
        for item in records
        if item.method_id == method_id and item.state == state
    )
    return tuple(sorted(matches, key=lambda item: (-item.created_order, item.result_id)))


def _nearest_rank_p95(samples: Sequence[int]) -> int:
    ordered = sorted(samples)
    rank = max(0, ((95 * len(ordered) + 99) // 100) - 1)
    return ordered[rank]


def _measure_filter(records: Sequence[_SyntheticCatalogRecord]) -> PerformanceMeasurement:
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
        canonical_json_bytes(
            [
                {
                    "result_id": item.result_id,
                    "method_id": item.method_id,
                    "state": item.state,
                }
                for item in page
            ]
        )
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


@contextmanager
def deny_external_network() -> Iterator[list[str]]:
    """Deny socket connections in this Python process during an offline run."""

    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_connect(instance: socket.socket, address: object) -> None:
        del instance
        attempts.append(repr(address))
        raise RuntimeError("external network disabled by E14 harness")

    def blocked_create_connection(*args: object, **kwargs: object) -> None:
        del kwargs
        attempts.append(repr(args[0] if args else "unknown"))
        raise RuntimeError("external network disabled by E14 harness")

    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection


def load_screenshot_manifest(path: Path) -> tuple[ScreenshotManifest, bytes]:
    content = path.read_bytes().removesuffix(b"\n")
    parsed = ScreenshotManifest.model_validate_json(content)
    if canonical_json_bytes(parsed) != content:
        raise ValueError("screenshot manifest must use canonical JSON")
    return parsed, content


def _privacy_clean(payload: bytes, sentinels: Sequence[bytes]) -> bool:
    folded = payload.lower()
    return all(sentinel.lower() not in folded for sentinel in sentinels)


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
    privacy_pass = _privacy_clean(privacy_payload, sentinels)

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
            status=EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED,
            detail="Measured locally against 10000 synthetic records; approved-host rerun required",
            evidence_ref=run_id,
        ),
        GateEvidence(
            gate_id=GateId.FIVE_PROVIDER_STUDY,
            status=EvidenceStatus.REQUIRED_EXTERNAL,
            detail="Five-provider task evidence has not been collected",
        ),
        GateEvidence(
            gate_id=GateId.INITIAL_RENDER,
            status=EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED,
            detail="Measured local projection serialization; approved-host browser evidence required",
            evidence_ref=run_id,
        ),
        GateEvidence(
            gate_id=GateId.NO_EXTERNAL_NETWORK,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if not network_attempts
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Python-process socket connections denied during the complete harness run",
            evidence_ref=run_id,
        ),
        GateEvidence(
            gate_id=GateId.PRIVACY_SENTINELS,
            status=(
                EvidenceStatus.OBSERVED_PASS
                if privacy_pass
                else EvidenceStatus.OBSERVED_FAIL
            ),
            detail="Forbidden synthetic identifiers, path, and sequence absent from gate output",
            evidence_ref=run_id,
        ),
        GateEvidence(
            gate_id=GateId.SCREENSHOTS,
            status=EvidenceStatus.FIXTURE_ONLY,
            detail="Synthetic screenshot state metadata only; reviewed browser captures required",
        ),
        GateEvidence(
            gate_id=GateId.STRESS_MEMORY,
            status=EvidenceStatus.OBSERVED_LOCAL_UNAPPROVED,
            detail="Measured peak Python allocations for the 100000-record stress fixture",
            evidence_ref=run_id,
        ),
    )
    return ProductGateReport(
        host_run=host_run,
        filter_performance=filter_measurement,
        initial_render=render_measurement,
        stress_memory=memory,
        screenshot_manifest_sha256=sha256_bytes(manifest_bytes),
        gate_evidence=tuple(sorted(evidence, key=lambda item: str(item.gate_id))),
        capability_enabled=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        raise SystemExit("usage: python -m traceback_runner.product_gates MANIFEST RUN_ID")
    report = run_foundation_gates(
        screenshot_manifest_path=Path(args[0]),
        run_id=args[1],
    )
    sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
