"""Offline tests for Streamlit session semantics."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import execution_label
from evidence_inspector.case_bundle import build_synthetic_case
from evidence_inspector.models import (
    AuditError,
    AuditResult,
    AuditStatus,
    ErrorCode,
    ExecutionMode,
    VerificationLevel,
    bind_claim,
)
from evidence_inspector.ui_state import (
    SessionStatus,
    UIState,
    commit_claim_edit,
    ensure_case,
    execute_audit,
    select_claim,
)


def _audit_for(
    state: UIState,
    *,
    execution_mode: ExecutionMode = ExecutionMode.FIXTURE,
) -> AuditResult:
    claim = state.selected_claim
    binding = bind_claim(claim, state.case.dataset_revision)
    tool_result = state.case.tool_results[0]
    return AuditResult(
        id=f"audit.{claim.id}.{claim.revision}",
        claim_id=claim.id,
        claim_revision=binding.claim_revision,
        claim_text_hash=binding.claim_text_hash,
        dataset_revision=binding.dataset_revision,
        status=AuditStatus.SUPPORTED,
        verification_level=VerificationLevel.SAMPLED_RECOMPUTED,
        summary="The bounded synthetic evidence supports this claim.",
        evidence_ids=(tool_result.id,),
        tool_results=(tool_result,),
        revised_text=claim.text,
        execution_mode=execution_mode,
        model_id="fixture-model",
        prompt_version="test-v1",
    )


def _runner_returning(result: AuditResult) -> Callable[..., AuditResult]:
    def runner(*_: object) -> AuditResult:
        return result

    return runner


def test_initialization_and_ordinary_state_operations_do_not_call_runner() -> None:
    case = build_synthetic_case()
    calls = 0

    def runner(*_: object) -> AuditResult:
        nonlocal calls
        calls += 1
        raise AssertionError("runner should not be called")

    state = UIState.for_case(case)
    select_claim(state, case.claims[1].id)
    ensure_case(state, case)
    assert state.selected_claim.id == case.claims[1].id
    assert state.status == SessionStatus.READY
    assert calls == 0
    assert runner  # The runner is never registered with ordinary reruns.


def test_real_demo_defaults_to_method_mismatch_claim() -> None:
    from evidence_inspector.case_bundle import load_case_bundle

    state = UIState.for_case(load_case_bundle("data/demo/case.json"))

    assert state.selected_claim_id == "claim.fragment-method-equivalence"


def test_execute_calls_runner_once_and_publishes_current_result() -> None:
    state = UIState.for_case(build_synthetic_case())
    result = _audit_for(state, execution_mode=ExecutionMode.LIVE)
    calls = 0

    def runner(case, claim) -> AuditResult:
        nonlocal calls
        calls += 1
        assert case.claim_by_id(claim.id) == claim
        return result

    assert execute_audit(state, runner) is result
    assert calls == 1
    assert state.status == SessionStatus.COMPLETE
    assert state.selected_audit is result
    assert not state.selected_audit_is_stale
    assert result.execution_mode.value == "live"


def test_edit_preserves_original_quote_and_marks_previous_result_stale() -> None:
    state = UIState.for_case(build_synthetic_case())
    original_claim = state.selected_claim
    result = _audit_for(state)
    execute_audit(state, _runner_returning(result))

    assert commit_claim_edit(state, f"{original_claim.text} Edited.")
    assert state.selected_claim.original_quote == original_claim.original_quote
    assert state.selected_claim.revision == original_claim.revision + 1
    assert state.selected_audit is result
    assert state.selected_audit_is_stale
    assert state.status == SessionStatus.READY


def test_same_text_does_not_increment_revision_or_make_result_stale() -> None:
    state = UIState.for_case(build_synthetic_case())
    result = _audit_for(state)
    execute_audit(state, _runner_returning(result))

    assert not commit_claim_edit(state, state.selected_claim.text)
    assert state.selected_claim.revision == 1
    assert not state.selected_audit_is_stale
    assert state.status == SessionStatus.COMPLETE


def test_invalid_empty_edit_does_not_replace_current_claim() -> None:
    state = UIState.for_case(build_synthetic_case())
    original = state.selected_claim

    with pytest.raises(ValueError, match="must not be empty"):
        commit_claim_edit(state, "   ")

    assert state.selected_claim is original


def test_rerun_after_edit_binds_to_new_claim_hash_and_revision() -> None:
    state = UIState.for_case(build_synthetic_case())
    old_result = _audit_for(state)
    execute_audit(state, _runner_returning(old_result))
    commit_claim_edit(state, "The measured synthetic mode is not 167 bp.")
    new_result = _audit_for(state, execution_mode=ExecutionMode.CACHED)

    execute_audit(state, _runner_returning(new_result))

    assert state.selected_audit is new_result
    assert not state.selected_audit_is_stale
    assert new_result.claim_revision == 2
    assert new_result.claim_text_hash != old_result.claim_text_hash
    assert new_result.execution_mode.value == "cached"


def test_mismatched_result_fails_closed_and_keeps_prior_stale_result() -> None:
    state = UIState.for_case(build_synthetic_case())
    old_result = _audit_for(state)
    execute_audit(state, _runner_returning(old_result))
    commit_claim_edit(state, "A newly edited claim.")

    assert execute_audit(state, _runner_returning(old_result)) is None
    assert state.status == SessionStatus.FAILED
    assert state.error is not None
    assert state.error.code == ErrorCode.STALE_RESULT
    assert state.selected_audit is old_result
    assert state.selected_audit_is_stale


def test_runner_error_and_timeout_are_safe_failures() -> None:
    state = UIState.for_case(build_synthetic_case())
    expected = AuditError(
        code=ErrorCode.INVALID_CITATION,
        message="The response cited an unknown source.",
        retryable=True,
        fix="Retry the review.",
    )
    assert execute_audit(state, lambda *_: expected) is None
    assert state.error == expected

    state.status = SessionStatus.READY

    def timeout(*_: object) -> AuditResult:
        raise TimeoutError("private provider detail")

    assert execute_audit(state, timeout) is None
    assert state.error is not None
    assert state.error.code == ErrorCode.MODEL_TIMEOUT
    assert "private provider detail" not in state.error.message


def test_case_revision_change_resets_session_state() -> None:
    first = build_synthetic_case()
    state = UIState.for_case(first)
    changed = first.model_copy(
        update={
            "case_id": "case.synthetic.v2",
            "dataset_id": "dataset.synthetic.v2",
        }
    )

    replacement = ensure_case(state, changed)

    assert replacement is not state
    assert replacement.case is changed
    assert replacement.audits == {}


def test_execution_mode_labels_are_explicit() -> None:
    assert execution_label(ExecutionMode.LIVE) == "Live"
    assert execution_label(ExecutionMode.CACHED) == "Cached"
    assert execution_label(ExecutionMode.FIXTURE) == "Fixture"


def _write_named_case(path: Path, *, case_id: str, dataset_id: str) -> None:
    case = build_synthetic_case().model_copy(
        update={"case_id": case_id, "dataset_id": dataset_id}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(case.model_dump_json())


def _run_default_app() -> AppTest:
    return AppTest.from_string(
        "from app import run_app\nrun_app()\n",
        default_timeout=10,
    ).run()


def _dataset_metric(app: AppTest) -> str:
    return next(metric.value for metric in app.metric if metric.label == "Dataset")


def test_app_default_selects_traceback_case_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured" / "case.json"
    _write_named_case(
        configured,
        case_id="case.environment.v1",
        dataset_id="dataset.environment.v1",
    )
    monkeypatch.setenv("TRACEBACK_CASE_PATH", str(configured))
    monkeypatch.chdir(tmp_path)

    app = _run_default_app()

    assert not app.exception
    assert _dataset_metric(app) == "dataset.environment.v1"


def test_app_default_selects_conventional_local_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACEBACK_CASE_PATH", raising=False)
    local_case = tmp_path / "data" / "local" / "case.json"
    _write_named_case(
        local_case,
        case_id="case.local.v1",
        dataset_id="dataset.local.v1",
    )
    monkeypatch.chdir(tmp_path)

    app = _run_default_app()

    assert not app.exception
    assert _dataset_metric(app) == "dataset.local.v1"


def test_app_default_selects_public_demo_case_when_local_data_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACEBACK_CASE_PATH", raising=False)
    demo_case = tmp_path / "data" / "demo" / "case.json"
    _write_named_case(
        demo_case,
        case_id="case.demo.v1",
        dataset_id="dataset.demo.v1",
    )
    monkeypatch.chdir(tmp_path)

    app = _run_default_app()

    assert not app.exception
    assert _dataset_metric(app) == "dataset.demo.v1"


def test_app_default_uses_synthetic_when_local_data_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACEBACK_CASE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    app = _run_default_app()

    assert not app.exception
    assert _dataset_metric(app) == "dataset.synthetic.v1"


def test_explicit_case_takes_precedence_over_invalid_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_path = tmp_path / "sensitive" / "missing-case.json"
    monkeypatch.setenv("TRACEBACK_CASE_PATH", str(sensitive_path))
    app = AppTest.from_string(
        """
from app import run_app
from evidence_inspector.case_bundle import build_synthetic_case

case = build_synthetic_case().model_copy(
    update={"dataset_id": "dataset.injected.v1"}
)
run_app(case=case)
""",
        default_timeout=10,
    ).run()

    assert not app.exception
    assert _dataset_metric(app) == "dataset.injected.v1"
    assert not app.error


def test_configured_case_load_failure_is_safe_and_does_not_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_path = tmp_path / "patient-name" / "missing-case.json"
    monkeypatch.setenv("TRACEBACK_CASE_PATH", str(sensitive_path))
    monkeypatch.chdir(tmp_path)

    app = _run_default_app()

    assert not app.exception
    assert len(app.error) == 1
    assert "configured case bundle could not be loaded" in app.error[0].value
    visible_text = " ".join(
        element.value
        for collection in (app.error, app.caption, app.markdown)
        for element in collection
    )
    assert str(sensitive_path) not in visible_text
    assert "patient-name" not in visible_text
    assert not app.metric
    assert not app.selectbox
