"""Pure session-state orchestration for the Traceback Streamlit interface.

Streamlit reruns the application for every widget interaction.  This module
keeps model execution behind one explicit function so ordinary reruns cannot
accidentally make provider calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .models import (
    AuditError,
    AuditResult,
    Case,
    Claim,
    ErrorCode,
    audit_is_stale,
    binding_is_current,
    revise_claim,
    validate_audit_result,
)


class SessionStatus(StrEnum):
    """Lifecycle of the one synchronous audit allowed in a session."""

    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


AuditRunner = Callable[[Case, Claim], AuditResult | AuditError]


@dataclass(slots=True)
class UIState:
    """Mutable UI state backed by immutable contract objects."""

    case: Case
    selected_claim_id: str
    claims: dict[str, Claim]
    status: SessionStatus = SessionStatus.READY
    audits: dict[str, AuditResult] = field(default_factory=dict)
    error: AuditError | None = None

    @classmethod
    def for_case(cls, case: Case) -> UIState:
        """Initialize state without executing checks or model calls."""

        return cls(
            case=case,
            selected_claim_id=case.claims[0].id,
            claims={claim.id: claim for claim in case.claims},
        )

    @property
    def selected_claim(self) -> Claim:
        return self.claims[self.selected_claim_id]

    @property
    def selected_audit(self) -> AuditResult | None:
        return self.audits.get(self.selected_claim_id)

    @property
    def selected_audit_is_stale(self) -> bool:
        audit = self.selected_audit
        return audit is not None and audit_is_stale(
            audit,
            claim=self.selected_claim,
            dataset_revision=self.case.dataset_revision,
        )

    def case_with_current_claims(self) -> Case:
        """Materialize the immutable case revision with current claim edits."""

        current_claims = tuple(self.claims[claim.id] for claim in self.case.claims)
        return Case.model_validate(
            {
                **self.case.model_dump(mode="python"),
                "claims": current_claims,
            }
        )


def select_claim(state: UIState, claim_id: str) -> None:
    """Select a known claim without performing any audit work."""

    if claim_id not in state.claims:
        raise KeyError(f"unknown claim ID: {claim_id}")
    state.selected_claim_id = claim_id
    state.error = None
    audit = state.selected_audit
    state.status = (
        SessionStatus.COMPLETE
        if audit is not None and not state.selected_audit_is_stale
        else SessionStatus.READY
    )


def commit_claim_edit(state: UIState, text: str) -> bool:
    """Commit an editor value and invalidate the binding of any prior audit.

    Returns true only when the claim changed.  ``revise_claim`` preserves the
    source quote and increments the revision.
    """

    if state.status == SessionStatus.RUNNING:
        raise RuntimeError("cannot edit a claim while an audit is running")
    current = state.selected_claim
    revised = revise_claim(current, text)
    if revised is current:
        return False
    state.claims[current.id] = revised
    state.status = SessionStatus.READY
    state.error = None
    return True


def _failure(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool,
    fix: str,
) -> AuditError:
    return AuditError(code=code, message=message, retryable=retryable, fix=fix)


def _error_from_exception(exc: Exception) -> AuditError:
    """Convert unexpected runner failures to non-sensitive UI errors."""

    if isinstance(exc, TimeoutError):
        return _failure(
            ErrorCode.MODEL_TIMEOUT,
            "The review exceeded its time limit; no result was published.",
            retryable=True,
            fix="Retry once the model service is responsive.",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return _failure(
            ErrorCode.INVALID_INPUT,
            "The review returned data that did not satisfy the evidence contract.",
            retryable=False,
            fix="Check the selected case inputs and reviewer output.",
        )
    return _failure(
        ErrorCode.MODEL_FAILURE,
        "The review could not be completed; no result was published.",
        retryable=True,
        fix="Verify model configuration and retry.",
    )


def execute_audit(state: UIState, runner: AuditRunner) -> AuditResult | None:
    """Run exactly one audit in response to an explicit UI action.

    The runner receives a validated case containing the current claim
    revisions.  A late or mismatched result fails closed and cannot replace a
    previously completed result.
    """

    if state.status == SessionStatus.RUNNING:
        state.error = _failure(
            ErrorCode.UNAVAILABLE,
            "An audit is already running in this session.",
            retryable=False,
            fix="Wait for the active audit to finish.",
        )
        return None

    state.status = SessionStatus.RUNNING
    state.error = None
    claim = state.selected_claim
    current_case = state.case_with_current_claims()

    try:
        outcome = runner(current_case, claim)
        if isinstance(outcome, AuditError):
            state.error = outcome
            state.status = SessionStatus.FAILED
            return None
        if not isinstance(outcome, AuditResult):
            raise TypeError("audit runner returned an unsupported result")
        if not binding_is_current(
            outcome.binding,
            claim=state.selected_claim,
            dataset_revision=state.case.dataset_revision,
        ):
            state.error = _failure(
                ErrorCode.STALE_RESULT,
                "The completed review does not match the current claim or dataset.",
                retryable=True,
                fix="Run the check again against the current claim.",
            )
            state.status = SessionStatus.FAILED
            return None
        validate_audit_result(outcome, current_case)
    except Exception as exc:  # Provider and validation boundaries fail closed.
        state.error = _error_from_exception(exc)
        state.status = SessionStatus.FAILED
        return None

    state.audits[claim.id] = outcome
    state.status = SessionStatus.COMPLETE
    return outcome


def ensure_case(state: UIState | None, case: Case) -> UIState:
    """Reuse session state only while it targets the same immutable case."""

    if (
        state is None
        or state.case.case_id != case.case_id
        or state.case.dataset_revision != case.dataset_revision
    ):
        return UIState.for_case(case)
    return state


__all__ = [
    "AuditRunner",
    "SessionStatus",
    "UIState",
    "commit_claim_edit",
    "ensure_case",
    "execute_audit",
    "select_claim",
]
