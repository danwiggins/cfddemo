"""Six-case synthetic evaluation harness with optional private live recording."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bedrock_chat.config import Settings
from bedrock_chat.responses import StatelessResponsesClient
from evidence_inspector.case_bundle import build_synthetic_case
from evidence_inspector.checks import compare_reference_ranges
from evidence_inspector.models import (
    AuditError,
    AuditResult,
    Case,
    CheckSelection,
    Claim,
    ToolName,
    ToolResult,
)
from evidence_inspector.reviewer import EvidenceReviewer

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "cases.json"
RESULTS_DIR = EVALS_DIR / "results"

_SAMPLE_ROWS = (
    {
        "cell_type_id": "immune",
        "parent_id": None,
        "is_leaf": True,
        "fraction": 0.20,
    },
    {
        "cell_type_id": "vascular",
        "parent_id": None,
        "is_leaf": True,
        "fraction": 0.01,
    },
)
_REFERENCE_ROWS = (
    {
        "cell_type_id": "immune",
        "min_fraction": 0.10,
        "max_fraction": 0.30,
        "cohort_id": "synthetic-cohort",
        "source_id": "source.synthetic-table",
        "assay": "synthetic assay",
    },
    {
        "cell_type_id": "vascular",
        "min_fraction": 0.02,
        "max_fraction": 0.08,
        "cohort_id": "synthetic-cohort",
        "source_id": "source.synthetic-table",
        "assay": "synthetic assay",
    },
)


class ScriptedResponsesClient:
    """Minimal provider double that preserves real reviewer behavior."""

    def __init__(self, outputs: Sequence[str], *, model_id: str = "fixture-model") -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.settings = Settings(model_id=model_id, region="offline")

    def create(self, input_messages: Any, **kwargs: Any) -> str:
        self.calls.append({"input_messages": input_messages, **kwargs})
        if not self.outputs:
            raise AssertionError("scripted provider output queue is empty")
        return self.outputs.pop(0)


def load_cases(path: Path = CASES_PATH) -> tuple[dict[str, Any], ...]:
    """Load and minimally validate the committed synthetic case definitions."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or len(payload) != 6:
        raise ValueError("evaluation suite must define exactly six cases")
    required = {
        "id",
        "claim_id",
        "claim_text",
        "expected_status",
        "expected_tools",
        "selection",
        "assessment",
    }
    identifiers: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("each evaluation case must use the exact harness fields")
        string_fields = required - {"expected_tools", "selection", "assessment"}
        if not all(isinstance(item[key], str) for key in string_fields):
            raise ValueError("evaluation identifiers and expectations must be strings")
        expected_tools = item["expected_tools"]
        if (
            not isinstance(expected_tools, list)
            or not expected_tools
            or any(not isinstance(tool, str) for tool in expected_tools)
            or len(set(expected_tools)) != len(expected_tools)
            or any(tool not in {member.value for member in ToolName} for tool in expected_tools)
        ):
            raise ValueError("expected_tools must contain unique known tool names")
        if item["id"] == "raw-aligned-noncomparability":
            if set(expected_tools) != {
                ToolName.SOURCE_REVIEW.value,
                ToolName.INSUFFICIENT_INPUTS.value,
            }:
                raise ValueError(
                    "raw-aligned-noncomparability must allow source_review "
                    "and insufficient_inputs"
                )
        elif len(expected_tools) != 1:
            raise ValueError("all other evaluation cases require exactly one tool")
        if not isinstance(item["selection"], dict) or not isinstance(
            item["assessment"], dict
        ):
            raise ValueError("selection and assessment must be JSON objects")
        identifiers.append(item["id"])
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("evaluation case IDs must be unique")
    return tuple(payload)


def case_definition(case_id: str) -> dict[str, Any]:
    """Resolve one exact committed case ID."""

    for definition in load_cases():
        if definition["id"] == case_id:
            return definition
    raise KeyError(f"unknown evaluation case ID: {case_id}")


def _case_with_claim(definition: Mapping[str, Any]) -> Case:
    case = build_synthetic_case()
    claims = []
    for claim in case.claims:
        if claim.id != definition["claim_id"]:
            claims.append(claim)
            continue
        text = str(definition["claim_text"])
        claims.append(
            claim.model_copy(
                update={
                    "text": text,
                    "revision": claim.revision + int(text != claim.text),
                }
            )
        )
    return Case.model_validate(
        {
            **case.model_dump(mode="python"),
            "claims": tuple(claims),
        }
    )


def _table_result() -> ToolResult:
    return compare_reference_ranges(
        _SAMPLE_ROWS,
        _REFERENCE_ROWS,
        sample_artifact_id="artifact.synthetic-sample-table.v1",
        reference_artifact_id="artifact.synthetic-reference-table.v1",
        known_source_ids=("source.synthetic-table",),
        result_id="result.synthetic-reference-comparison.v1",
    )


def deterministic_check(selection: CheckSelection) -> ToolResult:
    """Resolve deterministic synthetic evidence for numerical selections."""

    if selection.tool == ToolName.READ_LENGTH_SUMMARY:
        return build_synthetic_case().tool_results[0]
    if selection.tool == ToolName.COMPARE_REFERENCE_RANGES:
        return _table_result()
    raise ValueError(f"no external fixture executor for {selection.tool.value}")


def scripted_client(definition: Mapping[str, Any]) -> ScriptedResponsesClient:
    """Build the provider double for one committed deterministic case."""

    return ScriptedResponsesClient(
        (
            json.dumps(definition["selection"], separators=(",", ":")),
            json.dumps(definition["assessment"], separators=(",", ":")),
        )
    )


def deterministic_runner(case_id: str) -> Callable[[Case, Claim], AuditResult | AuditError]:
    """Return a UI-compatible runner using one scripted synthetic evaluation."""

    definition = case_definition(case_id)

    def run(case: Case, claim: Claim) -> AuditResult | AuditError:
        client = scripted_client(definition)
        return EvidenceReviewer(client).audit_safe(
            case,
            claim.id,
            deterministic_check,
        )

    return run


def run_case(
    definition: Mapping[str, Any],
    *,
    client: StatelessResponsesClient | ScriptedResponsesClient | None = None,
) -> dict[str, Any]:
    """Run one case and return metadata-only outcome suitable for recording."""

    case = _case_with_claim(definition)
    active_client = client or scripted_client(definition)
    started = time.monotonic()
    outcome = EvidenceReviewer(active_client).audit_safe(
        case,
        str(definition["claim_id"]),
        deterministic_check,
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    if isinstance(outcome, AuditError):
        actual_status = None
        actual_tool = None
        error_code = outcome.code.value
    else:
        actual_status = outcome.status.value
        actual_tool = (
            outcome.tool_results[0].tool.value
            if outcome.tool_results
            else ToolName.INSUFFICIENT_INPUTS.value
        )
        error_code = None
    passed = (
        actual_status == definition["expected_status"]
        and actual_tool in definition["expected_tools"]
    )
    return {
        "case_id": definition["id"],
        "passed": passed,
        "expected_status": definition["expected_status"],
        "actual_status": actual_status,
        "expected_tools": list(definition["expected_tools"]),
        "actual_tool": actual_tool,
        "error_code": error_code,
        "latency_ms": latency_ms,
    }


def run_suite(
    *,
    live: bool = False,
    client_factory: Callable[[], StatelessResponsesClient] | None = None,
) -> dict[str, Any]:
    """Run all six cases offline or against a stateless live provider."""

    if live:
        factory = client_factory or StatelessResponsesClient
        client = factory()
        settings = client.settings
        outcomes = [run_case(case, client=client) for case in load_cases()]
        mode = "live"
    else:
        settings = Settings(model_id="fixture-model", region="offline")
        outcomes = [run_case(case) for case in load_cases()]
        mode = "deterministic"
    return {
        "schema_version": "traceback-evals.v2",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "model_id": settings.model_id,
        "region": settings.region,
        "passed": all(item["passed"] for item in outcomes),
        "cases": outcomes,
    }


def record_live_outcomes(report: Mapping[str, Any], path: Path | None = None) -> Path:
    """Write metadata-only live outcomes under the ignored results directory."""

    if report.get("mode") != "live":
        raise ValueError("only live evaluation reports may be recorded")
    target = path or (
        RESULTS_DIR
        / f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    resolved_results = RESULTS_DIR.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_results):
        raise ValueError("live results must remain under ignored evals/results/")
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    resolved_target.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return resolved_target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="use configured Bedrock credentials and record metadata-only outcomes",
    )
    args = parser.parse_args(argv)
    report = run_suite(live=args.live)
    if args.live:
        target = record_live_outcomes(report)
        print(f"Recorded {target}")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
