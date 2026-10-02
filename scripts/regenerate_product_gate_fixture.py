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

# Fields that vary run to run or host to host. The structural comparison keeps
# their presence and shape but not their values.
_VOLATILE_VALUE_KEYS = frozenset(
    {
        "p95_us",
        "peak_bytes",
        "python_version",
        "operating_system",
        "machine",
        "processor",
    }
)


def run_live_report() -> ProductGateReport:
    """Run the live foundation harness with the fixture's frozen inputs."""

    return run_foundation_gates(
        screenshot_manifest_path=REPO_ROOT / SCREENSHOT_MANIFEST,
        run_id=RUN_ID,
        captured_at=CAPTURED_AT,
    )


def structural_projection(value: Any, key: str | None = None) -> Any:
    """Project a report dump onto what must not drift.

    Timing samples, memory peaks, host platform fields, and every digest
    (digests bind those volatile values) are reduced to their type or
    presence. Record counts, gate ids, statuses, details, targets and all
    other fields are compared exactly.
    """

    if key is not None and (key in _VOLATILE_VALUE_KEYS or key.endswith("sha256")):
        if isinstance(value, list):
            return ["<digest>" if item is not None else None for item in value]
        return None if value is None else f"<{type(value).__name__}>"
    if key == "samples_us":
        return {"sample_count": len(value)}
    if isinstance(value, dict):
        return {name: structural_projection(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [structural_projection(item) for item in value]
    return value


def main() -> int:
    report = run_live_report()
    destination = REPO_ROOT / REPORT_FIXTURE
    destination.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
