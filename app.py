"""Single-page Streamlit interface for the Traceback evidence inspector."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

from evidence_inspector.case_bundle import CaseBundleLoadError, load_default_case
from evidence_inspector.cell_origin_pipeline import CellOriginResultBundle
from evidence_inspector.fragmentomics import chart_rows
from evidence_inspector.models import (
    AuditError,
    AuditResult,
    Case,
    Claim,
    ErrorCode,
    ExecutionMode,
    ReadLengthSummaryValues,
    Source,
    ToolName,
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
FRAGMENT_SOURCE_SLIDE = Path("data/local/slides/fragment-length-source.png")
CELL_ORIGIN_RESULT = Path("data/local/cell-origin/result.json")


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


def _render_fragmentomics_evidence(st: Any, case: Case) -> None:
    """Render the computed distribution before any model interpretation."""

    result = next(
        (
            item
            for item in case.tool_results
            if item.tool == ToolName.READ_LENGTH_SUMMARY
            and item.status.value == "ok"
        ),
        None,
    )
    if result is None:
        return
    try:
        values = ReadLengthSummaryValues.model_validate(result.values)
        rows = chart_rows(values)
    except ValueError:
        st.error("The registered fragment-length result failed chart validation.")
        return

    st.subheader("Algorithmic regeneration")
    st.caption(
        "Computed from the immutable BAM-derived artifact. This panel uses raw "
        "query-sequence length; no fixed adapter subtraction is applied."
    )
    count, mode, median, tail = st.columns(4)
    count.metric("Accepted reads", f"{values.valid_read_count:,}")
    mode.metric("Raw mode", f"{values.mode_bp} bp")
    median.metric("Raw median", f"{values.median_bp:g} bp")
    tail.metric("Reads >1 kb", f"{values.fraction_gt_1000:.2%}")

    source_column, computed_column = st.columns(2)
    with source_column:
        st.markdown("**Presentation source**")
        if FRAGMENT_SOURCE_SLIDE.is_file():
            st.image(
                str(FRAGMENT_SOURCE_SLIDE),
                caption=(
                    "Slide-reported analysis. The right panel uses a fixed adapter "
                    "subtraction and is not recomputed evidence."
                ),
                use_container_width=True,
            )
        else:
            st.info("Optional source slide is not registered locally.")
    with computed_column:
        st.markdown("**Regenerated from BAM query lengths**")
        st.vega_lite_chart(
            list(rows),
            spec={
                "mark": {
                    "type": "area",
                    "line": {"color": "#0E7490"},
                    "color": "#67E8F9",
                },
                "encoding": {
                    "x": {
                        "field": "length_bp",
                        "type": "quantitative",
                        "title": "Raw query length (bp)",
                        "scale": {"domain": [50, 800]},
                    },
                    "y": {
                        "field": "read_count",
                        "type": "quantitative",
                        "title": "Read count per 5 bp",
                    },
                    "tooltip": [
                        {
                            "field": "length_bp",
                            "type": "quantitative",
                            "title": "Bin start (bp)",
                        },
                        {
                            "field": "read_count",
                            "type": "quantitative",
                            "title": "Reads",
                            "format": ",",
                        },
                    ],
                },
            },
            use_container_width=True,
        )
        st.caption(
            f"Verification: {_humanize(result.verification_level)}. "
            "Aligned span appears only after hg38 alignment succeeds."
        )


def _render_cell_origin_evidence(st: Any) -> None:
    """Render validated cell-origin estimates without inventing absent results."""

    if not CELL_ORIGIN_RESULT.is_file():
        with st.expander("Cell-origin deconvolution", expanded=False):
            st.info(
                "No validated cell-origin result is registered yet. Run the "
                "methylation → UXM → NNLS pipeline to populate this chart."
            )
        return

    try:
        bundle = CellOriginResultBundle.model_validate_json(
            CELL_ORIGIN_RESULT.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        st.error("The registered cell-origin result failed strict validation.")
        return
    result = bundle.result
    range_rows = [
        row.model_dump(mode="json")
        for row in bundle.charts.healthy_context_rows
    ]
    if range_rows:
        composition = [
            {
                "label": row["label"],
                "rank": rank,
                "percent": row["sample_percent"],
                "lower_percent": (
                    row["sample_lower_percent"]
                    if row["sample_lower_percent"] is not None
                    else row["sample_percent"]
                ),
                "upper_percent": (
                    row["sample_upper_percent"]
                    if row["sample_upper_percent"] is not None
                    else row["sample_percent"]
                ),
                "value_label": f"{row['sample_percent']:.1f}%",
            }
            for rank, row in enumerate(
                sorted(
                    range_rows,
                    key=lambda item: item["sample_percent"],
                    reverse=True,
                ),
                start=1,
            )
        ]
    else:
        composition = [
            {
                **row.model_dump(mode="json"),
                "value_label": f"{row.percent:.1f}%",
            }
            for row in bundle.charts.composition_rows
            if row.show_by_default and row.fraction >= 0.001
        ][:10]

    st.subheader("Cell-origin deconvolution")
    st.caption(
        "Realigned Nanopore CpG calls → fragment U/X/M classification → "
        "count-weighted NNLS. This is an analytical reconstruction, not a "
        "diagnostic result."
    )
    observed_markers = len(result.marker_counts)
    st.info(
        "Reproduction gap: the report describes ~17,300 qualifying fragments "
        "across 6,469 markers. This strict rerun produced "
        f"{result.provenance.classified_fragment_marker_count:,} classified "
        f"fragment–marker groups across {observed_markers:,} markers. The "
        "chart is computed, but the extraction settings or counting units are "
        "not yet reconciled."
    )
    left, right = st.columns((0.9, 1.25))
    with left:
        st.markdown("**Top estimated contributors**")
        st.vega_lite_chart(
            composition,
            spec={
                "height": {"step": 31},
                "encoding": {
                    "y": {
                        "field": "label",
                        "type": "nominal",
                        "sort": {"field": "rank", "order": "ascending"},
                        "title": None,
                        "axis": {"labelLimit": 155},
                    }
                },
                "layer": [
                    {
                        "mark": {
                            "type": "bar",
                            "cornerRadiusEnd": 5,
                            "color": "#0E7490",
                        },
                        "encoding": {
                            "x": {
                                "field": "percent",
                                "type": "quantitative",
                                "title": "Estimated contribution (%)",
                            },
                            "tooltip": [
                                {"field": "label", "title": "Cell type"},
                                {
                                    "field": "percent",
                                    "title": "Estimate",
                                    "format": ".1f",
                                },
                                {
                                    "field": "lower_percent",
                                    "title": "Bootstrap lower",
                                    "format": ".1f",
                                },
                                {
                                    "field": "upper_percent",
                                    "title": "Bootstrap upper",
                                    "format": ".1f",
                                },
                            ],
                        },
                    },
                    {
                        "mark": {
                            "type": "text",
                            "align": "left",
                            "dx": 5,
                            "fontWeight": 600,
                            "color": "#E2E8F0",
                        },
                        "encoding": {
                            "x": {
                                "field": "percent",
                                "type": "quantitative",
                            },
                            "text": {"field": "value_label"},
                        },
                    },
                ],
            },
            use_container_width=True,
        )
    with right:
        st.markdown("**Sample versus healthy plasma donors**")
        if not range_rows:
            st.info("No method-matched healthy reference table is registered.")
        else:
            st.vega_lite_chart(
                range_rows,
                spec={
                    "height": {"step": 34},
                    "encoding": {
                        "y": {
                            "field": "label",
                            "type": "nominal",
                            "sort": {
                                "field": "sample_percent",
                                "order": "descending",
                            },
                            "title": None,
                            "axis": {"labelLimit": 155},
                        },
                        "x": {
                            "type": "quantitative",
                            "scale": {"zero": True},
                            "title": "Estimated contribution (%)",
                        },
                    },
                    "layer": [
                        {
                            "mark": {
                                "type": "rule",
                                "strokeWidth": 3,
                                "color": "#94A3B8",
                            },
                            "encoding": {
                                "x": {"field": "healthy_min_percent"},
                                "x2": {"field": "healthy_max_percent"},
                                "tooltip": [
                                    {
                                        "field": "healthy_min_percent",
                                        "title": "Healthy minimum",
                                        "format": ".1f",
                                    },
                                    {
                                        "field": "healthy_max_percent",
                                        "title": "Healthy maximum",
                                        "format": ".1f",
                                    },
                                ],
                            },
                        },
                        {
                            "mark": {
                                "type": "bar",
                                "height": 11,
                                "cornerRadius": 5,
                                "color": "#67E8F9",
                                "opacity": 0.9,
                            },
                            "encoding": {
                                "x": {"field": "healthy_q1_percent"},
                                "x2": {"field": "healthy_q3_percent"},
                            },
                        },
                        {
                            "mark": {
                                "type": "tick",
                                "thickness": 2,
                                "size": 18,
                                "color": "#164E63",
                            },
                            "encoding": {
                                "x": {"field": "healthy_median_percent"}
                            },
                        },
                        {
                            "transform": [
                                {"filter": "datum.uncertainty_available"}
                            ],
                            "mark": {
                                "type": "rule",
                                "strokeWidth": 2,
                                "color": "#7C3AED",
                            },
                            "encoding": {
                                "x": {"field": "sample_lower_percent"},
                                "x2": {"field": "sample_upper_percent"},
                            },
                        },
                        {
                            "mark": {
                                "type": "point",
                                "filled": True,
                                "size": 135,
                                "stroke": "white",
                                "strokeWidth": 1.5,
                            },
                            "encoding": {
                                "x": {"field": "sample_percent"},
                                "color": {
                                    "field": "classification",
                                    "type": "nominal",
                                    "scale": {
                                        "domain": ["within", "below", "above"],
                                        "range": ["#0F766E", "#C2410C", "#C2410C"],
                                    },
                                    "legend": {"title": None, "orient": "top"},
                                },
                                "shape": {
                                    "field": "classification",
                                    "type": "nominal",
                                    "scale": {
                                        "domain": ["within", "below", "above"],
                                        "range": ["circle", "triangle-left", "triangle-right"],
                                    },
                                    "legend": None,
                                },
                                "tooltip": [
                                    {"field": "label", "title": "Cell type"},
                                    {
                                        "field": "sample_percent",
                                        "title": "Sample",
                                        "format": ".1f",
                                    },
                                    {
                                        "field": "classification",
                                        "title": "Range check",
                                    },
                                ],
                            },
                        },
                    ],
                },
                use_container_width=True,
            )

    diagnostics = result.deconvolution.diagnostics
    st.caption(
        "Grey = observed min–max · cyan = IQR · tick = median · "
        "dot = regenerated sample · purple whisker = bootstrap interval."
    )
    st.caption(
        f"{len(bundle.charts.composition_rows)} cell types · "
        f"residual L2 {diagnostics.residual_l2:.4g} · "
        f"{result.provenance.classified_fragment_marker_count:,} classified "
        f"fragment–marker observations · "
        f"{_humanize(result.provenance.verification_level)}"
    )


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
    _render_fragmentomics_evidence(st, active_case)
    _render_cell_origin_evidence(st)
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
