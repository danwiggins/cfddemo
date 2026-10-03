#!/usr/bin/env python3
"""Regenerate the frozen product-gates report fixture.

`tests/test_product_gates.py` loads this fixture instead of running the live
harness (about 10 s). Regenerate it with this script after any change to the
`ProductGateReport` contract or to `run_foundation_gates`; never hand-edit it.

    uv run python scripts/regenerate_product_gate_fixture.py

The slow test `test_live_harness_structure_matches_frozen_fixture` (run with
`pytest -m slow`) fails when the live harness drifts from the fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceback_runner.product_gates import (
    ProductGateReport,
    run_foundation_gates,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_MANIFEST = Path("tests/fixtures/product_gates/screenshot_manifest.json")
REPORT_FIXTURE = Path("tests/fixtures/product_gates/foundation_report.json")
RUN_ID = "gate_run_20260929"
CAPTURED_AT = datetime(2026, 9, 29, tzinfo=UTC)

# Values that vary run to run (timings, tracemalloc peak) or host to host
# (platform strings), and the digests that bind them. Every other field,
# including all other digests, is compared exactly.
_HOST_FIELDS = ("python_version", "operating_system", "machine", "processor")
_TIMED_MEASUREMENTS = ("filter_performance", "initial_render")
_VOLATILE_DIGESTS = (
    # host_run digest
    ("network_denial_evidence", "host_run_sha256"),
    ("privacy_sentinel_evidence", "host_run_sha256"),
    # digest of host_run + timings + memory
    ("privacy_sentinel_evidence", "output_payload_sha256"),
)
# Gates whose evidence_sha256 digests one of the volatile objects above.
_VOLATILE_GATE_EVIDENCE = frozenset(
    {
        "filter_performance",
        "initial_render",
        "stress_memory",
        "no_external_network",
        "privacy_sentinels",
    }
)
_MASK = "<volatile>"


def run_live_report() -> ProductGateReport:
    """Run the live foundation harness with the fixture's frozen inputs."""

    return run_foundation_gates(
        screenshot_manifest_path=REPO_ROOT / SCREENSHOT_MANIFEST,
        run_id=RUN_ID,
        captured_at=CAPTURED_AT,
    )


def structural_projection(report: ProductGateReport) -> dict[str, Any]:
    """Project a report onto what must not drift between runs and hosts.

    Masks only the volatile values listed above; timing sample lists keep their
    length. A renamed or removed field raises KeyError, which also means the
    fixture must be regenerated.
    """

    payload = report.model_dump(mode="json")
    for field in _HOST_FIELDS:
        payload["host_run"][field] = _MASK
    for name in _TIMED_MEASUREMENTS:
        measurement = payload[name]
        measurement["samples_us"] = {"sample_count": len(measurement["samples_us"])}
        measurement["p95_us"] = _MASK
    payload["stress_memory"]["peak_bytes"] = _MASK
    for section, field in _VOLATILE_DIGESTS:
        payload[section][field] = _MASK
    for item in payload["gate_evidence"]:
        if item["gate_id"] in _VOLATILE_GATE_EVIDENCE:
            item["evidence_sha256"] = _MASK
    return payload


def main() -> int:
    report = run_live_report()
    destination = REPO_ROOT / REPORT_FIXTURE
    destination.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
