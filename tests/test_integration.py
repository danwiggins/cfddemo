"""Offline integration coverage across Traceback's assembled boundaries."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from bedrock_chat.client import BedrockChatClient
from bedrock_chat.config import Settings
from bedrock_chat.responses import StatelessResponsesClient
from evals.harness import (
    ScriptedResponsesClient,
    case_definition,
    deterministic_runner,
    load_cases,
    run_case,
    run_suite,
)
from evidence_inspector.case_bundle import build_synthetic_case
from evidence_inspector.models import AuditError, ErrorCode
from evidence_inspector.reviewer import EvidenceReviewer
from evidence_inspector.ui_state import (
    SessionStatus,
    UIState,
    commit_claim_edit,
    execute_audit,
)

ROOT = Path(__file__).resolve().parents[1]


def _response(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def test_six_case_deterministic_evaluation_suite_passes() -> None:
    report = run_suite()
    definitions = load_cases()

    assert report["mode"] == "deterministic"
    assert report["schema_version"] == "traceback-evals.v2"
    assert report["passed"] is True
    assert len(report["cases"]) == 6
    assert {item["case_id"] for item in report["cases"]} == {
        "supported-length",
        "false-mode",
        "table-discrepancy",
        "method-drift",
        "causal-hypothesis",
        "raw-aligned-noncomparability",
    }
    assert all(item["error_code"] is None for item in report["cases"])
    raw_definition = next(
        item for item in definitions if item["id"] == "raw-aligned-noncomparability"
    )
    assert set(raw_definition["expected_tools"]) == {
        "source_review",
        "insufficient_inputs",
    }
    assert all(
        len(item["expected_tools"]) == 1
        for item in definitions
        if item["id"] != "raw-aligned-noncomparability"
    )


def test_claim_to_selected_evidence_to_validated_audit() -> None:
    result = run_case(case_definition("supported-length"))

    assert result["passed"] is True
    assert result["actual_tool"] == "read_length_summary"
    assert result["actual_status"] == "supported"


def test_method_drift_causal_and_insufficient_paths_remain_scoped() -> None:
    outcomes = {
        case_id: run_case(case_definition(case_id))
        for case_id in (
            "method-drift",
            "causal-hypothesis",
            "raw-aligned-noncomparability",
        )
    }

    assert outcomes["method-drift"]["actual_status"] == "contradicted"
    assert outcomes["method-drift"]["actual_tool"] == "source_review"
    assert outcomes["causal-hypothesis"]["actual_status"] == "hypothesis"
    assert outcomes["causal-hypothesis"]["actual_tool"] == "source_review"
    assert (
        outcomes["raw-aligned-noncomparability"]["actual_status"]
        == "insufficient_evidence"
    )
    assert (
        outcomes["raw-aligned-noncomparability"]["actual_tool"]
        == "insufficient_inputs"
    )


def test_raw_aligned_rubric_accepts_bounded_source_review_route() -> None:
    definition = deepcopy(case_definition("raw-aligned-noncomparability"))
    source = build_synthetic_case().source_by_id("source.synthetic-length")
    definition["selection"] = {
        "tool": "source_review",
        "reason": "Review the bounded registered passage without inferring biology.",
        "artifact_ids": [],
        "source_ids": [source.id],
    }
    definition["assessment"] = {
        "status": "insufficient_evidence",
        "summary": "The registered passage cannot establish the aligned-span or diagnostic claims.",
        "citations": [{"evidence_id": source.id, "quote": source.quote}],
        "numeric_assertions": [],
        "assumptions": [
            "Raw query length and aligned reference span are distinct measurements."
        ],
        "missing_validation": [
            "Comparable alignment processing and diagnostic validation are unavailable."
        ],
        "revised_text": "The raw query-length prefix is descriptive and does not resolve the asserted conclusions.",
        "next_checks": [],
    }

    result = run_case(definition)

    assert result["passed"] is True
    assert result["actual_status"] == "insufficient_evidence"
    assert result["actual_tool"] == "source_review"
    assert result["expected_tools"] == ["source_review", "insufficient_inputs"]


def test_edit_marks_old_result_stale_and_rerun_rebinds() -> None:
    case = build_synthetic_case()
    state = UIState.for_case(case)
    original_quote = state.selected_claim.original_quote

    first = execute_audit(state, deterministic_runner("supported-length"))
    assert first is not None
    assert state.status == SessionStatus.COMPLETE

    assert commit_claim_edit(
        state,
        "The synthetic subset's modal query-sequence length is 168 bp.",
    )
    assert state.selected_claim.original_quote == original_quote
    assert state.selected_audit_is_stale
    assert state.selected_claim.revision == 2

    second = execute_audit(state, deterministic_runner("false-mode"))
    assert second is not None
    assert second.status.value == "contradicted"
    assert second.claim_revision == 2
    assert second.claim_text_hash != first.claim_text_hash
    assert not state.selected_audit_is_stale


def test_model_prompts_and_recordable_outcomes_exclude_private_material() -> None:
    definition = case_definition("supported-length")
    client = ScriptedResponsesClient(
        (
            json.dumps(definition["selection"]),
            json.dumps(definition["assessment"]),
        )
    )
    case = build_synthetic_case()

    outcome = EvidenceReviewer(client).audit_safe(
        case,
        definition["claim_id"],
    )

    assert not isinstance(outcome, AuditError)
    model_visible = json.dumps(client.calls)
    forbidden = (
        "/Users/",
        "/private/",
        "Downloads",
        "private-read-",
        "AKIA",
        "A" * 80,
    )
    assert all(token not in model_visible for token in forbidden)
    report = json.dumps(run_suite())
    assert all(token not in report for token in forbidden)
    assert "claim_text" not in report
    assert "quote" not in report


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("citation", ErrorCode.INVALID_CITATION),
        ("number", ErrorCode.INVALID_NUMBER),
        ("unit", ErrorCode.INVALID_NUMBER),
        ("denominator", ErrorCode.INVALID_NUMBER),
        ("filter", ErrorCode.INVALID_NUMBER),
        ("definition", ErrorCode.INVALID_NUMBER),
    ],
)
def test_invalid_evidence_bindings_fail_closed_after_one_repair(
    mutation: str,
    expected_code: ErrorCode,
) -> None:
    definition = case_definition("supported-length")
    assessment = deepcopy(definition["assessment"])
    assertion = assessment["numeric_assertions"][0]
    if mutation == "citation":
        assessment["citations"][0]["evidence_id"] = "result.foreign"
    elif mutation == "number":
        assertion["value"] = 168
    elif mutation == "unit":
        assertion["unit"] = "percent"
    elif mutation == "denominator":
        assertion["denominator"] = "all observed records"
    elif mutation == "filter":
        assertion["filters"] = ["unregistered filter"]
    elif mutation == "definition":
        assertion["definition"] = "aligned reference span"
    else:
        raise AssertionError("unhandled mutation")

    bad = json.dumps(assessment)
    client = ScriptedResponsesClient(
        (
            json.dumps(definition["selection"]),
            bad,
            bad,
        )
    )
    result = EvidenceReviewer(client).audit_safe(
        build_synthetic_case(),
        definition["claim_id"],
    )

    assert isinstance(result, AuditError)
    assert result.code == expected_code
    assert len(client.calls) == 3


@pytest.mark.parametrize("failure_mode", ["transport_timeout", "late_response"])
def test_timeout_and_late_provider_output_fail_closed(failure_mode: str) -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        if failure_mode == "transport_timeout":
            raise TimeoutError("provider detail must not escape")
        clock.now = 15.0
        return _response(json.dumps(case_definition("supported-length")["selection"]))

    client = StatelessResponsesClient(
        Settings(model_id="fixture-model", region="offline"),
        transport=transport,
        clock=clock,
    )
    result = EvidenceReviewer(client, clock=clock).audit_safe(
        build_synthetic_case(),
        "claim.synthetic-length",
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.MODEL_TIMEOUT
    assert "provider detail" not in result.message
    assert calls == 1


def test_existing_bedrock_chat_flow_remains_intact() -> None:
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, json.loads(body)))
        return _response("pong")

    client = BedrockChatClient(
        Settings(model_id="openai.gpt-test", region="us-west-2"),
        transport=transport,
    )

    assert client.send("ping") == "pong"
    assert client.history == [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
    assert calls[0][0].endswith(".us-west-2.api.aws/openai/v1/responses")


def test_privacy_ignore_boundary_covers_local_inputs_and_live_results() -> None:
    candidates = (
        "data/local/private.bam",
        "data/private/report.pdf",
        "inputs/source.docx",
        "evals/results/live.json",
        ".env.local",
        ".streamlit/secrets.toml",
    )
    completed = subprocess.run(
        ["git", "check-ignore", *candidates],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert set(completed.stdout.splitlines()) == set(candidates)


def test_public_demo_bundles_exclude_paths_and_raw_identifiers() -> None:
    for relative_path in (
        "data/demo/case.json",
        "data/demo/cell-origin-result.json",
    ):
        encoded = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "/Users/" not in encoded
        assert "/private/" not in encoded
        assert "Downloads" not in encoded
        assert "bam_pass" not in encoded
        assert "nrconley" not in encoded.lower()
        assert "dan@" not in encoded.lower()


def test_streamlit_apptest_smoke_and_stale_rerun() -> None:
    app = AppTest.from_string(
        """
from app import run_app
from evals.harness import deterministic_runner
from evidence_inspector.case_bundle import build_synthetic_case

run_app(
    case=build_synthetic_case(),
    audit_runner=deterministic_runner("supported-length"),
)
""",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.button[0].label == "Run check"
    assert not app.button[0].disabled
    markdown_values = {item.value for item in app.markdown}
    assert any(
        "One blood draw. Two computed cfDNA signals." in value
        for value in markdown_values
    )
    assert any(
        "Not built in this demo" in value for value in markdown_values
    )
    assert "**How the fragment-length algorithm works**" in markdown_values
    assert "**Presentation source**" not in markdown_values
    assert any(
        item.value.startswith("Result 1 — My cfDNA is clean")
        for item in app.subheader
    )

    app.button[0].click().run()
    metrics = {(metric.label, metric.value) for metric in app.metric}
    assert ("Status", "Supported") in metrics
    assert ("Execution", "Live") in metrics
    assert not app.exception

    app.text_area[0].input(
        "The synthetic subset modal query-sequence length matches the computed mode."
    ).run()
    assert any("Stale assessment" in warning.value for warning in app.warning)
    assert "Current claim revision: 2" in {caption.value for caption in app.caption}

    app.button[0].click().run()
    assert not app.warning
    assert not app.exception
    assert "Claim revision 2" in {
        caption.value.split(" · ")[0] for caption in app.caption
    }


def test_case_definitions_are_synthetic_and_contain_no_local_paths() -> None:
    encoded = json.dumps(load_cases())

    assert "/Users/" not in encoded
    assert "/private/" not in encoded
    assert "Downloads" not in encoded
    assert "cfDNA_baseline" not in encoded
    assert "bam_pass" not in encoded


def test_registered_but_inapplicable_source_citation_fails_closed() -> None:
    case = build_synthetic_case()
    source = case.source_by_id("source.synthetic-paper-method")
    selection = {
        "tool": "source_review",
        "reason": "Use a registered but unrelated passage.",
        "artifact_ids": [],
        "source_ids": [source.id],
    }
    assessment = {
        "status": "supported",
        "summary": "The selected passage supports the scoped claim.",
        "citations": [{"evidence_id": source.id, "quote": source.quote}],
        "numeric_assertions": [],
        "assumptions": [],
        "missing_validation": [],
        "revised_text": "The scoped claim is supported by the selected passage.",
        "next_checks": [],
    }
    result = EvidenceReviewer(
        ScriptedResponsesClient(
            (json.dumps(selection), json.dumps(assessment))
        )
    ).audit_safe(case, "claim.synthetic-length")

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_CITATION


def test_default_reviewer_executes_advertised_table_capability() -> None:
    definition = case_definition("table-discrepancy")
    client = ScriptedResponsesClient(
        (
            json.dumps(definition["selection"]),
            json.dumps(definition["assessment"]),
        )
    )

    result = EvidenceReviewer(client).audit_safe(
        build_synthetic_case(),
        definition["claim_id"],
    )

    assert not isinstance(result, AuditError)
    assert result.status.value == "contradicted"
