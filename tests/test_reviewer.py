"""Offline tests for bounded selection, execution, and assessment."""

import json

import pytest

from bedrock_chat.config import Settings
from bedrock_chat.responses import ResponsesTimeout
from evidence_inspector.case_bundle import (
    SYNTHETIC_LENGTH_ARTIFACT_ID,
    SYNTHETIC_REFERENCE_TABLE_ID,
    SYNTHETIC_SAMPLE_TABLE_ID,
    build_synthetic_case,
)
from evidence_inspector.models import (
    AuditError,
    AuditStatus,
    ErrorCode,
    ToolName,
    VerificationLevel,
)
from evidence_inspector.reviewer import EvidenceReviewer, ReviewFailure


def _selection(tool: str, *, artifacts=(), sources=()) -> str:
    return json.dumps(
        {
            "tool": tool,
            "reason": "This is the applicable bounded check.",
            "artifact_ids": list(artifacts),
            "source_ids": list(sources),
        }
    )


def _length_assessment(*, value=167, unit="bp", prose_number=False) -> str:
    case = build_synthetic_case()
    result = case.tool_results[0]
    return json.dumps(
        {
            "status": "supported",
            "summary": (
                "The measured mode supports the scoped claim."
                if not prose_number
                else "The measured mode is 167 and supports the claim."
            ),
            "citations": [{"evidence_id": result.id, "quote": None}],
            "numeric_assertions": [
                {
                    "evidence_id": result.id,
                    "field": "mode_bp",
                    "value": value,
                    "unit": unit,
                    "definition": result.definitions["mode_bp"],
                    "denominator": result.denominator,
                    "filters": list(result.filters),
                }
            ],
            "assumptions": [],
            "missing_validation": [
                "The registered collection is partial and linkage is unverified."
            ],
            "revised_text": "The registered subset has the measured modal query length.",
            "next_checks": [],
        }
    )


def _source_assessment(case, *, wrong_quote=False) -> str:
    source_ids = (
        "source.synthetic-method-current",
        "source.synthetic-method-instructions",
    )
    sources = [case.source_by_id(source_id) for source_id in source_ids]
    return json.dumps(
        {
            "status": "contradicted",
            "summary": "The supplied passages describe different measurement methods.",
            "citations": [
                {
                    "evidence_id": source.id,
                    "quote": "invented quote" if wrong_quote else source.quote,
                }
                for source in sources
            ],
            "numeric_assertions": [],
            "assumptions": [],
            "missing_validation": [
                "Source review does not validate the underlying biology."
            ],
            "revised_text": (
                "The reproduction instruction differs from the current method."
            ),
            "next_checks": [],
        }
    )


def _table_assessment() -> str:
    case = build_synthetic_case()
    result = case.tool_results[1]
    return json.dumps(
        {
            "status": "contradicted",
            "summary": "At least one category falls outside its supplied range.",
            "citations": [{"evidence_id": result.id, "quote": None}],
            "numeric_assertions": [],
            "assumptions": [],
            "missing_validation": [
                "Observed cohort ranges are not clinical normal intervals."
            ],
            "revised_text": (
                "The supplied categories do not all fall within their ranges."
            ),
            "next_checks": [],
        }
    )


def _qualitative_assessment(status: str, *, citations=()) -> str:
    return json.dumps(
        {
            "status": status,
            "summary": "The available evidence cannot establish the claim.",
            "citations": list(citations),
            "numeric_assertions": [],
            "assumptions": [],
            "missing_validation": ["A direct applicable test is unavailable."],
            "revised_text": "The explanation remains an untested hypothesis.",
            "next_checks": [],
        }
    )


def _patterns(value):
    if isinstance(value, dict):
        return [
            pattern
            for key, item in value.items()
            for pattern in (
                [item] if key == "pattern" else _patterns(item)
            )
        ]
    if isinstance(value, list):
        return [pattern for item in value for pattern in _patterns(item)]
    return []


class _QueueClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.settings = Settings(model_id="openai.gpt-test")

    def create(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def test_two_stage_length_review_binds_and_validates_numeric_evidence() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            _length_assessment(),
        ]
    )

    audit = EvidenceReviewer(client).audit(case, "claim.synthetic-length")

    assert audit.status == AuditStatus.SUPPORTED
    assert audit.verification_level == VerificationLevel.SAMPLED_RECOMPUTED
    assert audit.claim_id == "claim.synthetic-length"
    assert audit.dataset_revision == case.dataset_revision
    assert audit.numeric_assertions[0].value == 167
    assert audit.evidence_ids == (case.tool_results[0].id,)
    assert audit.execution_mode.value == "live"
    assert len(client.calls) == 2
    assessment_prompt = client.calls[1]["messages"][0]["content"]
    assert '"bins"' not in assessment_prompt
    assert "1001" not in assessment_prompt


def test_provider_schema_excludes_digits_only_from_qualitative_fields() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            _length_assessment(),
        ]
    )

    audit = EvidenceReviewer(client).audit(case, "claim.synthetic-length")
    schema = client.calls[1]["json_schema"]
    properties = schema["properties"]
    no_digits = r"^[^0-9]*$"

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(properties)
    assert properties["summary"]["pattern"] == no_digits
    assert properties["revised_text"]["pattern"] == no_digits
    for field in ("assumptions", "missing_validation", "next_checks"):
        assert properties[field]["items"]["pattern"] == no_digits
    assert no_digits not in _patterns(properties["citations"])
    assert no_digits not in _patterns(properties["numeric_assertions"])
    assert audit.numeric_assertions[0].value == 167


def test_assessment_prompt_has_dynamic_numeric_evidence_allowlist() -> None:
    case = build_synthetic_case()
    result = case.tool_results[0]
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            _length_assessment(),
        ]
    )

    EvidenceReviewer(client).audit(case, "claim.synthetic-length")
    prompt = client.calls[1]["messages"][0]["content"]
    data = json.loads(prompt.split("\nDATA=", 1)[1])
    allowed = data["allowed_numeric_evidence"]

    assert allowed == [
        {
            "evidence_id": result.id,
            "field_names": [
                "overflow_count",
                "valid_read_count",
                "mode_bp",
                "median_bp",
                "fraction_100_150",
                "fraction_gt_1000",
            ],
        }
    ]
    assert "numeric_assertions MUST cite only evidence_id values listed" in prompt
    assert "MUST use only the exact field_names listed" in prompt
    assert "Source passages may be cited only by exact quote" in prompt
    assert "MUST NEVER be used as numeric_assertions" in prompt
    assert "source.synthetic-length" not in {
        item["evidence_id"] for item in allowed
    }
    assert data["citation_policy"] == {
        "mode": "tool_results_only",
        "allowed_evidence_ids": [result.id],
        "required_evidence_ids": [result.id],
        "context_source_ids": ["source.synthetic-length"],
    }
    assert "citations MUST contain only returned tool-result IDs" in prompt
    assert "Source excerpts are context only for numerical selections" in prompt


def test_mixed_source_and_tool_citations_fail_for_numerical_selection() -> None:
    case = build_synthetic_case()
    source = case.source_by_id("source.synthetic-length")
    invalid = json.loads(_length_assessment())
    invalid["citations"].insert(
        0,
        {"evidence_id": source.id, "quote": source.quote},
    )
    invalid_output = json.dumps(invalid)
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            invalid_output,
            invalid_output,
        ]
    )

    result = EvidenceReviewer(client).audit_safe(
        case, "claim.synthetic-length"
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_CITATION
    assert len(client.calls) == 3


def test_tool_only_table_citation_preserves_recomputed_verification() -> None:
    case = build_synthetic_case()
    table_result = case.tool_results[1]
    client = _QueueClient(
        [
            _selection(
                "compare_reference_ranges",
                artifacts=(
                    SYNTHETIC_SAMPLE_TABLE_ID,
                    SYNTHETIC_REFERENCE_TABLE_ID,
                ),
            ),
            _table_assessment(),
        ]
    )

    audit = EvidenceReviewer(client).audit(
        case, "claim.synthetic-method-equivalence"
    )

    assert audit.evidence_ids == (table_result.id,)
    assert audit.verification_level == VerificationLevel.RECOMPUTED


def test_assessment_prompt_requires_empty_numeric_assertions_without_tools() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            _selection("insufficient_inputs"),
            _qualitative_assessment("insufficient_evidence"),
        ]
    )

    EvidenceReviewer(client).audit(case, "claim.synthetic-length")
    prompt = client.calls[1]["messages"][0]["content"]
    data = json.loads(prompt.split("\nDATA=", 1)[1])

    assert data["tool_results"] == []
    assert data["allowed_numeric_evidence"] == []
    assert "numeric_assertions MUST be empty when tool_results is empty" in prompt


@pytest.mark.parametrize(
    ("field", "digit_text"),
    [
        ("summary", "The measured mode is 167."),
        ("assumptions", "This assumes 1 registered subset."),
        ("missing_validation", "Validation step 2 is missing."),
        ("revised_text", "The measured mode is 167 bp."),
        ("next_checks", "Run 1 additional check."),
    ],
)
def test_digit_bearing_qualitative_fields_fail_closed(
    field: str,
    digit_text: str,
) -> None:
    invalid = json.loads(_length_assessment())
    invalid[field] = (
        digit_text
        if field in {"summary", "revised_text"}
        else [digit_text]
    )
    invalid_output = json.dumps(invalid)
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            invalid_output,
            invalid_output,
        ]
    )

    result = EvidenceReviewer(client).audit_safe(
        build_synthetic_case(), "claim.synthetic-length"
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_NUMBER
    assert len(client.calls) == 3


def test_source_review_resolves_exact_ids_and_stays_reported() -> None:
    case = build_synthetic_case()
    source_ids = (
        "source.synthetic-method-current",
        "source.synthetic-method-instructions",
    )
    client = _QueueClient(
        [
            _selection("source_review", sources=source_ids),
            _source_assessment(case),
        ]
    )

    audit = EvidenceReviewer(client).audit(
        case, "claim.synthetic-method-equivalence"
    )

    assert audit.status == AuditStatus.CONTRADICTED
    assert audit.verification_level == VerificationLevel.REPORTED
    assert audit.evidence_ids == source_ids
    assert audit.tool_results[0].tool == ToolName.SOURCE_REVIEW


def test_source_review_rejects_registered_but_claim_inapplicable_selection() -> None:
    case = build_synthetic_case()
    irrelevant = case.source_by_id("source.synthetic-paper-method")
    invalid_selection = _selection(
        "source_review",
        sources=(irrelevant.id,),
    )
    client = _QueueClient([invalid_selection, invalid_selection])

    result = EvidenceReviewer(client).audit_safe(
        case, "claim.synthetic-length"
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_CITATION
    assert len(client.calls) == 2
    assert irrelevant.quote not in client.calls[0]["messages"][0]["content"]


def test_assessment_rejects_claim_source_not_selected_for_review() -> None:
    case = build_synthetic_case()
    selected_id = "source.synthetic-method-current"
    unselected = case.source_by_id("source.synthetic-method-instructions")
    invalid_assessment = _qualitative_assessment(
        "contradicted",
        citations=(
            {
                "evidence_id": unselected.id,
                "quote": unselected.quote,
            },
        ),
    )
    client = _QueueClient(
        [
            _selection("source_review", sources=(selected_id,)),
            invalid_assessment,
            invalid_assessment,
        ]
    )

    result = EvidenceReviewer(client).audit_safe(
        case, "claim.synthetic-method-equivalence"
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_CITATION
    assert len(client.calls) == 3
    assessment_prompt = client.calls[1]["messages"][0]["content"]
    assert unselected.id not in assessment_prompt
    assert unselected.quote not in assessment_prompt


def test_invalid_quote_uses_single_repair_then_succeeds() -> None:
    case = build_synthetic_case()
    source_ids = (
        "source.synthetic-method-current",
        "source.synthetic-method-instructions",
    )
    client = _QueueClient(
        [
            _selection("source_review", sources=source_ids),
            _source_assessment(case, wrong_quote=True),
            _source_assessment(case),
        ]
    )

    audit = EvidenceReviewer(client).audit(
        case, "claim.synthetic-method-equivalence"
    )

    assert audit.status == AuditStatus.CONTRADICTED
    assert len(client.calls) == 3
    assert "Repair the invalid JSON response" in client.calls[2]["messages"][0]["content"]
    assert client.calls[2]["timeout_seconds"] <= 8


def test_unknown_citation_fails_closed_after_single_repair() -> None:
    case = build_synthetic_case()
    invalid = json.loads(_length_assessment())
    invalid["citations"] = [{"evidence_id": "result.foreign", "quote": None}]
    bad_output = json.dumps(invalid)
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            bad_output,
            bad_output,
        ]
    )

    result = EvidenceReviewer(client).audit_safe(case, "claim.synthetic-length")

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_CITATION
    assert len(client.calls) == 3


def test_selection_repair_consumes_only_repair_for_entire_audit() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            "{bad json",
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            _length_assessment(prose_number=True),
        ]
    )

    with pytest.raises(ReviewFailure) as caught:
        EvidenceReviewer(client).audit(case, "claim.synthetic-length")

    assert caught.value.error.code == ErrorCode.INVALID_NUMBER
    assert len(client.calls) == 3


def test_wrong_number_and_unit_fail_closed_after_one_repair() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            _selection(
                "read_length_summary",
                artifacts=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            _length_assessment(value=168),
            _length_assessment(unit="percent"),
        ]
    )

    result = EvidenceReviewer(client).audit_safe(case, "claim.synthetic-length")

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_NUMBER
    assert len(client.calls) == 3


def test_unknown_selection_id_never_reaches_check_executor() -> None:
    case = build_synthetic_case()
    called = False

    def executor(_selection):
        nonlocal called
        called = True
        return case.tool_results[0]

    invalid = _selection("read_length_summary", artifacts=("foreign-artifact",))
    client = _QueueClient([invalid, invalid])

    result = EvidenceReviewer(client).audit_safe(
        case, "claim.synthetic-length", executor
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.MODEL_FAILURE
    assert called is False


def test_model_timeout_maps_to_deterministic_typed_error() -> None:
    case = build_synthetic_case()
    client = _QueueClient([ResponsesTimeout("late")])

    result = EvidenceReviewer(client).audit_safe(case, "claim.synthetic-length")

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.MODEL_TIMEOUT
    assert result.retryable is True
    assert len(client.calls) == 1


def test_insufficient_selection_cannot_publish_supported_result() -> None:
    case = build_synthetic_case()
    client = _QueueClient(
        [
            _selection("insufficient_inputs"),
            _qualitative_assessment("supported"),
            _qualitative_assessment("supported"),
        ]
    )

    result = EvidenceReviewer(client).audit_safe(case, "claim.synthetic-length")

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.MODEL_FAILURE
    assert len(client.calls) == 3


def test_untested_causal_claim_is_forced_to_hypothesis() -> None:
    case = build_synthetic_case()
    source_ids = ("source.synthetic-causal", "source.synthetic-table")
    citations = [
        {
            "evidence_id": source_id,
            "quote": case.source_by_id(source_id).quote,
        }
        for source_id in source_ids
    ]
    client = _QueueClient(
        [
            _selection("source_review", sources=source_ids),
            _qualitative_assessment("hypothesis", citations=citations),
        ]
    )

    audit = EvidenceReviewer(client).audit(case, "claim.synthetic-causal")

    assert audit.status == AuditStatus.HYPOTHESIS
    assert audit.verification_level == VerificationLevel.REPORTED


def test_prompt_blocks_private_paths_before_any_model_call() -> None:
    case = build_synthetic_case()
    claim = case.claims[0].model_copy(update={"text": "Inspect /Users/alice/private.bam"})
    changed_case = case.model_copy(
        update={"claims": (claim,) + case.claims[1:]}
    )
    client = _QueueClient([])

    result = EvidenceReviewer(client).audit_safe(
        changed_case, "claim.synthetic-length"
    )

    assert isinstance(result, AuditError)
    assert result.code == ErrorCode.INVALID_INPUT
    assert client.calls == []
