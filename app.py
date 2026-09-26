"""Single-page Streamlit interface for the Traceback evidence inspector."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from typing import Any

from evidence_inspector.case_bundle import CaseBundleLoadError, load_default_case
from evidence_inspector.models import (
    AuditError,
    AuditResult,
    Case,
    Claim,
    ErrorCode,
    ExecutionMode,
    Source,
    ToolResult,
)
from evidence_inspector.ui_state import (
    AuditRunner,
    SessionStatus,
    UIState,
    commit_claim_edit,
    ensure_case,
    execute_audit,
    select_claim,
)

SESSION_KEY = "traceback_ui_state"


def _humanize(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).replace("_", " ").title()


def execution_label(mode: ExecutionMode) -> str:
    """Return the explicit provenance label required by the UI contract."""

    return {
        ExecutionMode.LIVE: "Live",
        ExecutionMode.CACHED: "Cached",
        ExecutionMode.FIXTURE: "Fixture",
    }[mode]


def _render_header(st: Any) -> None:
    st.title("Traceback")
    st.caption(
        "Inspect the evidence chain behind a claim. This prototype does not "
        "diagnose disease or validate an assay."
    )


def _default_runner(case: Case, claim: Claim) -> AuditResult | AuditError:
    """Load the optional reviewer only after the user presses Check."""

    try:
        reviewer_module = import_module("evidence_inspector.reviewer")
        responses_module = import_module("bedrock_chat.responses")
        reviewer = reviewer_module.EvidenceReviewer(
            responses_module.StatelessResponsesClient()
        )
    except (AttributeError, ImportError, ModuleNotFoundError, ValueError):
        return AuditError(
            code=ErrorCode.UNAVAILABLE,
            message="The live reviewer is unavailable or not configured.",
            retryable=False,
            fix="Configure AWS credentials, model, region, and the reviewer modules.",
        )
    return reviewer.audit_safe(case, claim.id)


def _write_items(st: Any, heading: str, items: Iterable[str]) -> None:
    values = tuple(items)
    if not values:
        return
    st.markdown(f"**{heading}**")
    for item in values:
        st.markdown(f"- {item}")


def _render_case_scope(st: Any, case: Case) -> None:
    manifest = case.manifest
    st.subheader("Case scope")
    left, middle, right = st.columns(3)
    left.metric("Dataset", case.dataset_id)
    middle.metric(
        "Sample linkage",
        _humanize(manifest.sample_linkage.status),
    )
    right.metric("Trimming", _humanize(manifest.trimming.status))

    st.caption(
        f"Dataset revision: `{case.dataset_revision[:12]}…` · "
        f"{'Partial collection' if manifest.partial_collection else 'Complete registered collection'}"
    )
    st.caption(f"Linkage: {manifest.sample_linkage.operator_rationale}")
    st.caption(f"Processing: {manifest.trimming.processing_detail}")

    with st.expander("Available checks", expanded=False):
        for capability in manifest.capabilities:
            label = "Available" if capability.available else "Unavailable"
            st.markdown(f"**{_humanize(capability.name)} — {label}**")
            if capability.unavailable_reason:
                st.caption(capability.unavailable_reason)


def _source_heading(source: Source) -> str:
    locator = f"{_humanize(source.locator.kind)} {source.locator.value}"
    return f"{source.document_id} · {locator}"


def _render_source(st: Any, source: Source) -> None:
    with st.expander(_source_heading(source), expanded=False):
        st.caption(source.id)
        st.write(source.quote)
        if source.table_row is not None:
            st.markdown("**Exact table row**")
            st.code(source.table_row, language=None)


def _render_tool_result(st: Any, result: ToolResult) -> None:
    st.markdown(
        f"**{_humanize(result.tool)}** · {_humanize(result.status)}"
        + (
            f" · {_humanize(result.verification_level)}"
            if result.verification_level is not None
            else ""
        )
    )
    if result.values:
        st.json(dict(result.values))
    if result.denominator:
        st.caption(f"Denominator: {result.denominator}")
    _write_items(st, "Filters", result.filters)
    _write_items(st, "Limitations", result.limitations)
    with st.expander(f"Provenance · {result.id}", expanded=False):
        st.json(result.provenance.model_dump(mode="json"))


def _render_numeric_assertions(st: Any, audit: AuditResult) -> None:
    if not audit.numeric_assertions:
        return
    st.markdown("**Validated measurements**")
    for assertion in audit.numeric_assertions:
        denominator = (
            f"; denominator: {assertion.denominator}"
            if assertion.denominator is not None
            else ""
        )
        st.markdown(
            f"- `{assertion.field}` = **{assertion.value} {assertion.unit}** "
            f"({assertion.definition}{denominator}) "
            f"— evidence `{assertion.evidence_id}`"
        )


def _render_citations(st: Any, case: Case, audit: AuditResult) -> None:
    st.subheader("Evidence and citations")
    source_by_id = {source.id: source for source in case.sources}
    result_by_id = {result.id: result for result in audit.tool_results}
    for evidence_id in audit.evidence_ids:
        source = source_by_id.get(evidence_id)
        if source is not None:
            _render_source(st, source)
            continue
        result = result_by_id.get(evidence_id)
        if result is not None:
            _render_tool_result(st, result)
            continue
        st.error(f"Evidence `{evidence_id}` is unavailable in this case.")


def _render_audit(st: Any, state: UIState, audit: AuditResult) -> None:
    stale = state.selected_audit_is_stale
    if stale:
        st.warning(
            "Stale assessment — this result is retained for reference but does "
            "not match the current claim or dataset."
        )

    st.subheader("Assessment")
    status, verification, execution = st.columns(3)
    status.metric("Status", _humanize(audit.status))
    verification.metric("Verification", _humanize(audit.verification_level))
    execution.metric("Execution", execution_label(audit.execution_mode))
    st.caption(
        f"Claim revision {audit.claim_revision} · "
        f"claim hash `{audit.claim_text_hash[:12]}…`"
    )
    st.write(audit.summary)
    _render_numeric_assertions(st, audit)

    st.markdown("**Revised wording**")
    st.info(audit.revised_text)
    _write_items(st, "Assumptions", audit.assumptions)
    _write_items(st, "Missing validation", audit.missing_validation)
    _write_items(st, "Next checks", audit.next_checks)
    _render_citations(st, state.case, audit)


def _render_error(st: Any, error: AuditError) -> None:
    retry = "Retryable" if error.retryable else "Not retryable without a change"
    st.error(f"{error.code.value}: {error.message}")
    st.caption(f"{retry}. {error.fix}")


def _commit_editor(st: Any, state: UIState, editor_key: str) -> None:
    """Streamlit callback: commit text without starting an audit."""

    try:
        commit_claim_edit(state, st.session_state[editor_key])
    except (ValueError, RuntimeError) as exc:
        state.status = SessionStatus.FAILED
        state.error = AuditError(
            code=ErrorCode.INVALID_INPUT,
            message=str(exc),
            retryable=False,
            fix="Enter a non-empty claim within the allowed length.",
        )


def _render_claim_controls(st: Any, state: UIState) -> bool:
    claim_ids = tuple(claim.id for claim in state.case.claims)
    claim_labels = {
        claim.id: f"{_humanize(claim.kind)} · {claim.id}"
        for claim in state.case.claims
    }
    selected = st.selectbox(
        "Claim",
        options=claim_ids,
        index=claim_ids.index(state.selected_claim_id),
        format_func=claim_labels.__getitem__,
        key="traceback_claim_selector",
    )
    if selected != state.selected_claim_id:
        select_claim(state, selected)

    claim = state.selected_claim
    st.markdown("**Original source quote (immutable)**")
    st.info(claim.original_quote)
    for source_id in claim.source_ids:
        _render_source(st, state.case.source_by_id(source_id))

    editor_key = f"traceback_claim_editor:{claim.id}"
    if editor_key not in st.session_state:
        st.session_state[editor_key] = claim.text
    editor_value = st.text_area(
        "Claim to check",
        key=editor_key,
        height=120,
        max_chars=4_000,
        on_change=_commit_editor,
        args=(st, state, editor_key),
        disabled=state.status == SessionStatus.RUNNING,
        help="Editing commits a new claim revision and makes the old assessment stale.",
    )
    st.caption(f"Current claim revision: {state.selected_claim.revision}")
    return editor_value == state.selected_claim.text


def run_app(
    *,
    case: Case | None = None,
    audit_runner: AuditRunner | None = None,
    streamlit_module: Any | None = None,
) -> None:
    """Render the app with injectable case, reviewer, and Streamlit facade."""

    if streamlit_module is None:
        import streamlit as streamlit_module

    st = streamlit_module
    st.set_page_config(page_title="Traceback", page_icon="🔎", layout="centered")
    if case is not None:
        active_case = case
    else:
        try:
            active_case = load_default_case()
        except CaseBundleLoadError:
            _render_header(st)
            st.error(
                "INVALID_INPUT: The configured case bundle could not be loaded."
            )
            st.caption(
                "Verify that the configured bundle exists and satisfies the case "
                "contract. No fallback case was loaded."
            )
            return

    state = ensure_case(st.session_state.get(SESSION_KEY), active_case)
    st.session_state[SESSION_KEY] = state
    runner = audit_runner or _default_runner

    _render_header(st)
    _render_case_scope(st, active_case)
    st.divider()
    editor_is_committed = _render_claim_controls(st, state)

    pressed = st.button(
        "Run check",
        type="primary",
        disabled=state.status == SessionStatus.RUNNING or not editor_is_committed,
        help="Starts one review. Other interactions do not call the model.",
    )
    if pressed:
        with st.spinner("Checking the claim against registered evidence…"):
            execute_audit(state, runner)

    if state.error is not None:
        _render_error(st, state.error)
    if state.selected_audit is not None:
        st.divider()
        _render_audit(st, state, state.selected_audit)
    else:
        st.caption(
            "No assessment has been run for this claim. Results are session-only."
        )


if __name__ == "__main__":
    run_app()
