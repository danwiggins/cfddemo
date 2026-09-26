"""Offline coverage for the public recorded-assessment mode."""

from evidence_inspector.case_bundle import load_case_bundle
from evidence_inspector.demo_replay import replay_assessment
from evidence_inspector.models import (
    AuditError,
    AuditStatus,
    ExecutionMode,
    revise_claim,
)


def test_every_public_claim_has_a_valid_recorded_assessment() -> None:
    case = load_case_bundle("data/demo/case.json")

    audits = [replay_assessment(case, claim) for claim in case.claims]

    assert all(not isinstance(audit, AuditError) for audit in audits)
    assert [audit.status for audit in audits if not isinstance(audit, AuditError)] == [
        AuditStatus.SUPPORTED,
        AuditStatus.CONTRADICTED,
        AuditStatus.INSUFFICIENT_EVIDENCE,
    ]
    assert all(
        audit.execution_mode == ExecutionMode.FIXTURE
        for audit in audits
        if not isinstance(audit, AuditError)
    )


def test_recorded_mode_rejects_edited_wording() -> None:
    case = load_case_bundle("data/demo/case.json")
    edited = revise_claim(case.claims[0], "A materially different claim.")

    result = replay_assessment(case, edited)

    assert isinstance(result, AuditError)
    assert "edited" in result.message
