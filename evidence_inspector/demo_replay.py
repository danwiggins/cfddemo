"""Recorded, offline assessment replay for the public Traceback demo."""

from __future__ import annotations

from .models import (
    AuditError,
    AuditResult,
    AuditStatus,
    Case,
    Claim,
    ErrorCode,
    ExecutionMode,
    NumericAssertion,
    ToolName,
    VerificationLevel,
    bind_claim,
    validate_audit_result,
)

MODEL_ID = "recorded-demo-review"
PROMPT_VERSION = "traceback-recorded-demo-v1"

EXPECTED_TEXT = {
    "claim.prepared-length-mode": (
        "The registered prepared subset’s modal query-sequence length is 211 bp."
    ),
    "claim.fragment-method-equivalence": (
        "Section 6’s subtract-45 instructions use the same fragment-length method "
        "as the updated section 3.1."
    ),
    "claim.negligible-gdna": (
        "The prepared query-length subset establishes negligible genomic-DNA "
        "contamination."
    ),
}


def _unavailable(message: str, fix: str) -> AuditError:
    return AuditError(
        code=ErrorCode.UNAVAILABLE,
        message=message,
        retryable=False,
        fix=fix,
    )


def replay_assessment(case: Case, claim: Claim) -> AuditResult | AuditError:
    """Replay a validated assessment without making a provider call."""

    expected = EXPECTED_TEXT.get(claim.id)
    if expected is None:
        return _unavailable(
            "No recorded assessment exists for this claim.",
            "Select one of the three registered demo claims.",
        )
    if claim.text != expected:
        return _unavailable(
            "Recorded mode cannot assess edited wording.",
            "Restore the original claim or run locally with Bedrock credentials.",
        )

    if claim.id == "claim.prepared-length-mode":
        result = next(
            item
            for item in case.tool_results
            if item.tool == ToolName.READ_LENGTH_SUMMARY
        )
        assertion = NumericAssertion(
            evidence_id=result.id,
            field="mode_bp",
            value=result.values["mode_bp"],
            unit=result.units["mode_bp"],
            definition=result.definitions["mode_bp"],
            denominator=result.denominator,
            filters=result.filters,
        )
        audit = _build_audit(
            case,
            claim,
            status=AuditStatus.SUPPORTED,
            verification_level=result.verification_level or VerificationLevel.REPORTED,
            summary=(
                "The deterministic read-length check supports the scoped "
                "measurement."
            ),
            evidence_ids=(result.id,),
            numeric_assertions=(assertion,),
            tool_results=(result,),
            missing_validation=(
                "The registered collection is partial and sample linkage is "
                "unverified.",
            ),
            revised_text=(
                "In the registered prepared subset, the modal raw query-sequence "
                "length is 211 bp."
            ),
        )
    elif claim.id == "claim.fragment-method-equivalence":
        source_ids = (
            "source.report-section-3.1-method",
            "source.report-section-6-instructions",
        )
        audit = _build_audit(
            case,
            claim,
            status=AuditStatus.CONTRADICTED,
            verification_level=VerificationLevel.REPORTED,
            summary=(
                "The two cited passages specify different fragment-length "
                "definitions."
            ),
            evidence_ids=source_ids,
            missing_validation=(
                "Source review identifies a reproduction mismatch; it does not "
                "validate the underlying biology.",
            ),
            revised_text=(
                "Section 6’s fixed adapter subtraction differs from the updated "
                "aligned-reference-span method in section 3.1."
            ),
        )
    else:
        source_id = "source.report-section-3.1-interpretation"
        audit = _build_audit(
            case,
            claim,
            status=AuditStatus.INSUFFICIENT_EVIDENCE,
            verification_level=VerificationLevel.REPORTED,
            summary=(
                "The supplied interpretation is consistent with low long-fragment "
                "contamination, but a partial subset cannot establish the condition "
                "for the whole sample."
            ),
            evidence_ids=(source_id,),
            missing_validation=(
                "Whole-sample coverage and verified sample linkage are unavailable.",
            ),
            revised_text=(
                "The registered subset is consistent with low high-molecular-weight "
                "genomic-DNA contamination."
            ),
        )
    return validate_audit_result(audit, case)


def _build_audit(
    case: Case,
    claim: Claim,
    *,
    status: AuditStatus,
    verification_level: VerificationLevel,
    summary: str,
    evidence_ids: tuple[str, ...],
    missing_validation: tuple[str, ...],
    revised_text: str,
    numeric_assertions: tuple[NumericAssertion, ...] = (),
    tool_results: tuple = (),
) -> AuditResult:
    binding = bind_claim(claim, case.dataset_revision)
    return AuditResult(
        id=f"audit.recorded.{claim.id.split('.')[-1]}",
        **binding.model_dump(),
        status=status,
        verification_level=verification_level,
        summary=summary,
        evidence_ids=evidence_ids,
        numeric_assertions=numeric_assertions,
        tool_results=tool_results,
        assumptions=(),
        missing_validation=missing_validation,
        revised_text=revised_text,
        next_checks=(),
        execution_mode=ExecutionMode.FIXTURE,
        model_id=MODEL_ID,
        prompt_version=PROMPT_VERSION,
    )


__all__ = ["replay_assessment"]
