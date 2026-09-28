"""Single-page Streamlit interface for the Traceback evidence inspector."""

from __future__ import annotations

import os
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

from evidence_inspector.case_bundle import CaseBundleLoadError, load_default_case
from evidence_inspector.cell_origin_pipeline import CellOriginResultBundle
from evidence_inspector.copy_number import (
    EVENT_THRESHOLD_LOG2,
    CopyNumberResultBundle,
)
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
CELL_ORIGIN_RESULTS = (
    Path("data/local/cell-origin/result.json"),
    Path("data/demo/cell-origin-result.json"),
)
COPY_NUMBER_RESULTS = (
    Path("data/local/copy-number/result.json"),
    Path("data/demo/copy-number-result.json"),
)


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


def _render_development_result_banner(st: Any) -> None:
    """Keep development qualification and intended use visible with each result."""

    st.warning(
        "Development sample · Research use only (RUO) · Unqualified · "
        "Not a diagnostic result"
    )


def _inject_design_system(st: Any) -> None:
    """Apply the deck's visual system to the standalone demo."""

    st.markdown(
        """
<style>
:root {
  --ink: #1B1B2B;
  --ink-soft: #2A2A45;
  --muted: #6B6B7B;
  --paper: #FAFAFA;
  --surface: #FFFFFF;
  --line: #E3E3E8;
  --teal: #1B7F79;
  --teal-soft: #E4F1F0;
  --red: #B3262E;
  --red-soft: #F7E8E9;
  --gold: #C9A227;
}

.stApp {
  background: var(--paper);
  color: var(--ink);
}

[data-testid="stHeader"] {
  background: rgba(250, 250, 250, 0.92);
}

.block-container {
  max-width: 1180px;
  padding-top: 2rem;
  padding-bottom: 5rem;
}

h1, h2, h3, [data-testid="stHeadingWithActionElements"] {
  color: var(--ink) !important;
  font-family: Cambria, Georgia, serif !important;
  letter-spacing: -0.02em;
}

p, li, label, [data-testid="stCaptionContainer"] {
  font-family: Calibri, Arial, sans-serif;
}

.traceback-hero {
  background: var(--ink);
  border-radius: 26px;
  box-shadow: 0 22px 55px rgba(27, 27, 43, 0.15);
  color: white;
  margin-bottom: 2.1rem;
  overflow: hidden;
  padding: 2.7rem 3.2rem 2.6rem;
  position: relative;
}

.traceback-eyebrow, .section-kicker {
  color: #76C7C1;
  font-family: Calibri, Arial, sans-serif;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  margin: 0 0 0.9rem;
  text-transform: uppercase;
}

.section-kicker {
  color: var(--teal);
  margin: 2.4rem 0 0.35rem;
}

.traceback-hero h1 {
  color: white !important;
  font-family: Cambria, Georgia, serif !important;
  font-size: clamp(3.1rem, 7vw, 5.2rem);
  line-height: 0.92;
  margin: 0;
}

.traceback-hero h2 {
  color: white !important;
  font-family: Cambria, Georgia, serif !important;
  font-size: clamp(1.5rem, 3vw, 2.25rem);
  line-height: 1.08;
  margin: 1.35rem 0 1rem;
  max-width: 820px;
}

.traceback-hero .hero-copy {
  color: #C9C9D6;
  font-family: Calibri, Arial, sans-serif;
  font-size: 1.08rem;
  line-height: 1.5;
  max-width: 780px;
}

.hero-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
  margin-top: 1.55rem;
}

.hero-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.7rem;
  margin-top: 1.2rem;
}

.hero-action {
  border-radius: 999px;
  color: white !important;
  font-family: Calibri, Arial, sans-serif;
  font-size: 0.9rem;
  font-weight: 700;
  padding: 0.65rem 1rem;
  text-decoration: none !important;
}

.hero-action.primary {
  background: var(--teal);
}

.hero-action.secondary {
  border: 1px solid rgba(255, 255, 255, 0.28);
}

.truth-strip {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  margin: -1.2rem auto 2.2rem;
  max-width: 1080px;
  overflow: hidden;
  position: relative;
}

.truth-strip > div {
  padding: 0.95rem 1.1rem;
}

.truth-strip > div + div {
  border-left: 1px solid var(--line);
}

.truth-strip strong {
  color: var(--ink);
  display: block;
  font-family: Cambria, Georgia, serif;
  margin-bottom: 0.2rem;
}

.truth-strip span {
  color: var(--muted);
  font-family: Calibri, Arial, sans-serif;
  font-size: 0.86rem;
}

.truth-strip .real strong { color: var(--teal); }
.truth-strip .recorded strong { color: var(--gold); }
.truth-strip .unbuilt strong { color: var(--red); }

.judge-moment {
  background: linear-gradient(135deg, var(--teal-soft), #FFFFFF);
  border: 1px solid #B7D9D6;
  border-radius: 20px;
  margin-bottom: 2.4rem;
  padding: 1.35rem 1.5rem 0.35rem;
}

.review-flow {
  align-items: stretch;
  background: var(--ink);
  border-radius: 16px;
  color: white;
  display: grid;
  gap: 0;
  grid-template-columns: 1fr auto 1fr auto 1fr;
  margin-top: 1rem;
  overflow: hidden;
}

.review-flow > div {
  padding: 1.2rem 1.25rem;
}

.review-flow small {
  color: #76C7C1;
  display: block;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  margin-bottom: 0.4rem;
  text-transform: uppercase;
}

.review-flow strong {
  display: block;
  font-family: Cambria, Georgia, serif;
  font-size: 1.05rem;
  margin-bottom: 0.3rem;
}

.review-flow p {
  color: #C9C9D6;
  line-height: 1.4;
  margin: 0;
}

.review-flow .arrow {
  align-self: center;
  color: #76C7C1;
  font-size: 1.3rem;
}

.hero-tag {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 999px;
  color: white;
  font-family: Calibri, Arial, sans-serif;
  font-size: 0.82rem;
  padding: 0.44rem 0.75rem;
}

.story-card, .pipeline-step, .interpretation-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  height: 100%;
  padding: 1.15rem 1.2rem;
}

.story-card strong, .pipeline-step strong, .interpretation-card strong {
  color: var(--ink);
  display: block;
  font-family: Cambria, Georgia, serif;
  font-size: 1.08rem;
  margin-bottom: 0.4rem;
}

.story-card p, .pipeline-step p, .interpretation-card p {
  color: var(--muted);
  line-height: 1.42;
  margin: 0;
}

.story-card.active {
  background: var(--teal-soft);
  border-color: #B7D9D6;
}

.story-card.pending {
  background: #F4F4F7;
}

.card-label, .step-number {
  color: var(--teal);
  font-family: Calibri, Arial, sans-serif;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  margin-bottom: 0.55rem;
  text-transform: uppercase;
}

.step-number {
  background: var(--teal);
  border-radius: 999px;
  color: white;
  display: inline-grid;
  height: 1.7rem;
  letter-spacing: 0;
  margin-bottom: 0.7rem;
  place-items: center;
  width: 1.7rem;
}

.measured-note {
  background: var(--teal-soft);
  border-radius: 14px;
  color: var(--ink-soft);
  font-family: Calibri, Arial, sans-serif;
  line-height: 1.45;
  margin: 0.6rem 0 1.1rem;
  padding: 1rem 1.15rem;
}

.measured-note strong {
  color: var(--teal);
}

.limitation-note {
  background: var(--red-soft);
  border-radius: 14px;
  color: var(--ink-soft);
  font-family: Calibri, Arial, sans-serif;
  line-height: 1.45;
  margin: 1rem 0;
  padding: 1rem 1.15rem;
}

.limitation-note strong {
  color: var(--red);
}

div[data-testid="stMetric"] {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 0.85rem 1rem;
}

div[data-testid="stMetricValue"] {
  color: var(--ink);
  font-family: Cambria, Georgia, serif;
}

div[data-testid="stExpander"] {
  background: var(--surface);
  border-color: var(--line);
  border-radius: 14px;
}

.stButton > button[kind="primary"] {
  background: var(--teal);
  border: 0;
  border-radius: 999px;
  font-weight: 700;
  padding-left: 1.4rem;
  padding-right: 1.4rem;
}

.stButton > button[kind="primary"]:hover {
  background: #126B66;
}

hr {
  border-color: var(--line) !important;
  margin: 2.8rem 0 !important;
}

@media (max-width: 760px) {
  .block-container { padding: 1rem 1rem 3rem; }
  .traceback-hero { padding: 2.2rem 1.45rem; }
  .truth-strip { grid-template-columns: 1fr; margin-top: -1rem; }
  .truth-strip > div + div {
    border-left: 0;
    border-top: 1px solid var(--line);
  }
  .review-flow { grid-template-columns: 1fr; }
  .review-flow .arrow {
    justify-self: center;
    transform: rotate(90deg);
  }
}
</style>
""",
        unsafe_allow_html=True,
    )


def _render_header(st: Any) -> None:
    st.markdown(
        """
<section class="traceback-hero">
  <p class="traceback-eyebrow">Healthcare AI Hackathon · Research demo</p>
  <h1>Traceback</h1>
  <h2>One blood draw. Three computed cfDNA signals. Every claim traceable.</h2>
  <p class="hero-copy">
    A local-first pipeline that turns Oxford Nanopore reads into fragment-length,
    methylation cell-origin, and chromosome-dosage evidence, then uses AI to test
    the interpretation against the measured result and its source.
  </p>
  <div class="hero-tags">
    <span class="hero-tag">Real MinION data</span>
    <span class="hero-tag">Deterministic bioinformatics</span>
    <span class="hero-tag">AI evidence review</span>
    <span class="hero-tag">Research use only</span>
  </div>
  <div class="hero-actions">
    <a class="hero-action primary" href="#ai-review">Watch AI catch the mismatch ↓</a>
    <a class="hero-action secondary" href="#readout-1">Explore the results</a>
    <a class="hero-action secondary" href="https://github.com/danwiggins/cfddemo" target="_blank">View GitHub ↗</a>
  </div>
</section>
""",
        unsafe_allow_html=True,
    )


def _render_truth_strip(st: Any, *, replay_mode: bool) -> None:
    """State exactly what a judge is seeing."""

    review_detail = (
        "Validated AI assessment replay; no provider call"
        if replay_mode
        else "Live bounded review through Amazon Bedrock"
    )
    st.markdown(
        f"""
<div class="truth-strip">
  <div class="real">
    <strong>Real</strong>
    <span>One MinION run and three computed bioinformatics readouts</span>
  </div>
  <div class="recorded">
    <strong>{"Recorded" if replay_mode else "Live"}</strong>
    <span>{review_detail}</span>
  </div>
  <div class="unbuilt">
    <strong>Not built</strong>
    <span>Validated tumor-fraction calling or clinical diagnosis</span>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_signal_overview(st: Any) -> None:
    """Explain the biological premise and honest build scope."""

    st.markdown(
        '<p class="section-kicker">Why cfDNA</p>',
        unsafe_allow_html=True,
    )
    st.subheader("Dying cells shed DNA into blood—and each fragment carries clues")
    st.write(
        "Cell-free DNA is a mixture of short fragments released by tissues across "
        "the body. A single methylation-aware sequencing run can expose three "
        "different signals. This demo computes three exploratory development readouts."
    )
    length, methylation, copy_number = st.columns(3)
    with length:
        st.markdown(
            """
<div class="story-card active">
  <div class="card-label">Built · Readout 1</div>
  <strong>Fragment length</strong>
  <p>Plots the measured raw query-sequence-length distribution without classifying the sample.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with methylation:
        st.markdown(
            """
<div class="story-card active">
  <div class="card-label">Built · Readout 2</div>
  <strong>Methylation barcode</strong>
  <p>Fits an atlas-conditioned source-composition estimate from measured CpG patterns.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with copy_number:
        st.markdown(
            """
<div class="story-card active">
  <div class="card-label">Experimental · Readout 3</div>
  <strong>Chromosome dosage</strong>
  <p>Plots sample-internal chromosome medians against an unvalidated visualization boundary; it cannot determine gains or losses.</p>
</div>
""",
            unsafe_allow_html=True,
        )

    st.markdown(
        '<p class="section-kicker">What AI does</p>',
        unsafe_allow_html=True,
    )
    st.subheader("The algorithms calculate. The AI reviews the claim.")
    st.markdown(
        """
<div class="review-flow">
  <div>
    <small>Deterministic code</small>
    <strong>Compute the evidence</strong>
    <p>Filter reads, measure fragments, classify methylation, and fit the mixture.</p>
  </div>
  <span class="arrow">→</span>
  <div>
    <small>Bounded AI review</small>
    <strong>Test the written claim</strong>
    <p>Select one allowed check and compare the claim with registered evidence.</p>
  </div>
  <span class="arrow">→</span>
  <div>
    <small>Traceable output</small>
    <strong>Cite what supports it</strong>
    <p>Attach exact measurements, source passages, definitions, and limitations.</p>
  </div>
</div>
""",
        unsafe_allow_html=True,
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
    st.markdown(
        '<p class="section-kicker">The registered run</p>',
        unsafe_allow_html=True,
    )
    st.subheader("What went into this analysis")
    left, middle, right = st.columns(3)
    left.metric("Dataset", case.dataset_id)
    middle.metric(
        "Sample linkage",
        _humanize(manifest.sample_linkage.status),
    )
    right.metric("Trimming", _humanize(manifest.trimming.status))

    with st.expander("Data provenance and available checks", expanded=False):
        st.caption(
            f"Dataset revision: `{case.dataset_revision[:12]}…` · "
            f"{'Partial collection' if manifest.partial_collection else 'Complete registered collection'}"
        )
        st.caption(f"Linkage: {manifest.sample_linkage.operator_rationale}")
        st.caption(f"Processing: {manifest.trimming.processing_detail}")
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
            if item.tool == ToolName.READ_LENGTH_SUMMARY and item.status.value == "ok"
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

    st.markdown(
        '<span id="readout-1"></span><p class="section-kicker">Readout 1 · Fragmentomics</p>',
        unsafe_allow_html=True,
    )
    _render_development_result_banner(st)
    st.subheader("Result 1 — Measured fragment-length distribution")
    st.caption(
        "cfDNA fragment-length distribution · ONT R10.4.1 · computed from the "
        "immutable BAM-derived artifact. Raw query-sequence length is shown; "
        "no fixed adapter subtraction is applied."
    )
    count, mode, median, tail = st.columns(4)
    count.metric("Accepted reads", f"{values.valid_read_count:,}")
    mode.metric("Raw mode", f"{values.mode_bp} bp")
    median.metric("Raw median", f"{values.median_bp:g} bp")
    tail.metric("Reads >1 kb", f"{values.fraction_gt_1000:.2%}")
    st.markdown(
        """
<div class="measured-note">
  <strong>What is actually measured:</strong>
  raw query-sequence length for each accepted primary BAM record. The chart,
  mode, median, and long-fragment fraction all use that same denominator.
</div>
""",
        unsafe_allow_html=True,
    )

    explanation_column, computed_column = st.columns((0.8, 1.2))
    with explanation_column:
        st.markdown("**How the fragment-length algorithm works**")
        st.markdown(
            """
1. **Stream the BAM** one alignment at a time.
2. **Keep eligible reads** using explicit primary-alignment and sequence rules.
3. **Measure raw query length** directly from each accepted read—no inferred or fixed adapter subtraction.
4. **Bin the lengths** in 5 bp intervals, then calculate the mode, median, and long-fragment fraction.
"""
        )
        st.markdown(
            """
<div class="interpretation-card">
  <strong>Why this is auditable</strong>
  <p>Every number comes from the same explicit set of BAM records. Re-running the same artifact produces the same result.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with computed_column:
        st.markdown("**Computed fragment-length distribution**")
        st.vega_lite_chart(
            list(rows),
            spec={
                "mark": {
                    "type": "area",
                    "line": {"color": "#1B7F79", "strokeWidth": 2},
                    "color": "#B7D9D6",
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
                "config": {
                    "axis": {
                        "domainColor": "#C9C9D6",
                        "gridColor": "#E3E3E8",
                        "labelColor": "#6B6B7B",
                        "titleColor": "#1B1B2B",
                    },
                    "view": {"stroke": None},
                },
                "background": "#FFFFFF",
            },
            use_container_width=True,
            theme=None,
        )
        st.caption(
            f"Verification: {_humanize(result.verification_level)}. "
            "Aligned span appears only after hg38 alignment succeeds."
        )
    st.markdown(
        """
<div class="interpretation-card">
  <div class="card-label">How to read the result</div>
  <strong>Describe the measured distribution without classifying the sample</strong>
  <p>
    This chart reports raw query-sequence lengths, their distribution, and the
    measured long-fragment fraction. No protocol-matched comparator or validated
    decision threshold is applied, so this view does not determine tumor
    contribution or classify sample contamination.
  </p>
</div>
""",
        unsafe_allow_html=True,
    )
    st.caption(
        "Research/feasibility interpretation, not a clinical diagnostic. "
        "The algorithm reports the measured distribution; it does not diagnose cancer."
    )


def _render_cell_origin_evidence(st: Any) -> None:
    """Render validated cell-origin estimates without inventing absent results."""

    result_path = next(
        (path for path in CELL_ORIGIN_RESULTS if path.is_file()),
        None,
    )
    if result_path is None:
        with st.expander("Cell-origin deconvolution", expanded=False):
            st.info(
                "No validated cell-origin result is registered yet. Run the "
                "methylation → UXM → NNLS pipeline to populate this chart."
            )
        return

    try:
        bundle = CellOriginResultBundle.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        st.error("The registered cell-origin result failed strict validation.")
        return
    result = bundle.result
    range_rows = [
        row.model_dump(mode="json") for row in bundle.charts.healthy_context_rows
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

    st.markdown(
        '<p class="section-kicker">Readout 2 · Methylation</p>',
        unsafe_allow_html=True,
    )
    _render_development_result_banner(st)
    st.subheader("Estimated cfDNA source composition")
    st.caption(
        "Realigned Nanopore CpG calls → fragment U/X/M classification → "
        "count-weighted NNLS. Estimates are conditioned on the registered atlas "
        "and are not a diagnostic result."
    )
    st.markdown("**How the cell-origin algorithm works**")
    extract, match, classify, solve = st.columns(4)
    with extract:
        st.markdown(
            """
<div class="pipeline-step">
  <div class="step-number">1</div>
  <strong>Extract methylation</strong>
  <p>Read CpG modification calls from the hg38-aligned Nanopore BAM.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with match:
        st.markdown(
            """
<div class="pipeline-step">
  <div class="step-number">2</div>
  <strong>Match markers</strong>
  <p>Intersect each fragment with the curated Loyfer cell-type marker atlas.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with classify:
        st.markdown(
            """
<div class="pipeline-step">
  <div class="step-number">3</div>
  <strong>Classify U / X / M</strong>
  <p>Label each fragment–marker pair from its unmethylated CpG fraction.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with solve:
        st.markdown(
            """
<div class="pipeline-step">
  <div class="step-number">4</div>
  <strong>Solve the mixture</strong>
  <p>Use non-negative least squares to find the cell mixture that best explains the counts.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    st.caption(
        "Bootstrap resampling repeats the solve to estimate uncertainty. "
        "The bars below are computed outputs, not copied presentation values."
    )
    observed_markers = len(result.marker_counts)
    fragments, overlaps, classified, markers = st.columns(4)
    fragments.metric(
        "Input fragments",
        f"{result.provenance.input_fragment_count:,}",
    )
    overlaps.metric(
        "Marker overlaps",
        f"{result.provenance.marker_overlap_count:,}",
    )
    classified.metric(
        "Classified groups",
        f"{result.provenance.classified_fragment_marker_count:,}",
    )
    markers.metric("Observed markers", f"{observed_markers:,}")
    st.markdown(
        f"""
<div class="limitation-note">
  <strong>Known reproduction gap:</strong>
  the report describes ~17,300 qualifying fragments across 6,469 markers.
  This strict rerun produced
  {result.provenance.classified_fragment_marker_count:,} classified
  fragment–marker groups across {observed_markers:,} markers. The chart is
  computed, but the extraction settings or counting units are not reconciled.
</div>
""",
        unsafe_allow_html=True,
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
                            "color": "#1B7F79",
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
                            "color": "#1B1B2B",
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
                "config": {
                    "axis": {
                        "domainColor": "#C9C9D6",
                        "gridColor": "#E3E3E8",
                        "labelColor": "#6B6B7B",
                        "titleColor": "#1B1B2B",
                    },
                    "view": {"stroke": None},
                },
                "background": "#FFFFFF",
            },
            use_container_width=True,
            theme=None,
        )
    with right:
        st.markdown(
            "**Development estimate versus observed 23-donor reference cohort**"
        )
        if not range_rows:
            st.info("No method-matched observed reference cohort is registered.")
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
                                "color": "#C9C9D6",
                            },
                            "encoding": {
                                "x": {"field": "healthy_min_percent"},
                                "x2": {"field": "healthy_max_percent"},
                                "tooltip": [
                                    {
                                        "field": "healthy_min_percent",
                                        "title": "Observed cohort minimum",
                                        "format": ".1f",
                                    },
                                    {
                                        "field": "healthy_max_percent",
                                        "title": "Observed cohort maximum",
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
                                "color": "#B7D9D6",
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
                                "color": "#1B7F79",
                            },
                            "encoding": {"x": {"field": "healthy_median_percent"}},
                        },
                        {
                            "transform": [{"filter": "datum.uncertainty_available"}],
                            "mark": {
                                "type": "rule",
                                "strokeWidth": 2,
                                "color": "#6B6B7B",
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
                                    "value": "#1B7F79",
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
                                        "title": "Position in observed range",
                                    },
                                ],
                            },
                        },
                    ],
                    "config": {
                        "axis": {
                            "domainColor": "#C9C9D6",
                            "gridColor": "#E3E3E8",
                            "labelColor": "#6B6B7B",
                            "titleColor": "#1B1B2B",
                        },
                        "view": {"stroke": None},
                    },
                    "background": "#FFFFFF",
                },
                use_container_width=True,
                theme=None,
            )

    diagnostics = result.deconvolution.diagnostics
    st.caption(
        "Grey = observed min–max · pale teal = IQR · tick = median · "
        "dot = regenerated sample · dark whisker = bootstrap stability interval. "
        "The 23-donor range is descriptive, method-specific context—not a "
        "clinical reference interval."
    )
    st.caption(
        f"{len(bundle.charts.composition_rows)} cell types · "
        f"residual L2 {diagnostics.residual_l2:.4g} · "
        f"{result.provenance.classified_fragment_marker_count:,} classified "
        f"fragment–marker observations · "
        f"{_humanize(result.provenance.verification_level)}"
    )


def _render_copy_number_evidence(st: Any) -> None:
    """Render isolated exploratory whole-chromosome relative dosage QC."""

    result_path = next(
        (path for path in COPY_NUMBER_RESULTS if path.is_file()),
        None,
    )
    if result_path is None:
        return
    try:
        bundle = CopyNumberResultBundle.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        st.error("The registered relative-dosage result failed strict validation.")
        return

    rows = [
        {
            **row.model_dump(mode="json"),
            "label": row.chromosome.removeprefix("chr"),
        }
        for row in bundle.chromosomes
    ]
    largest = max(bundle.chromosomes, key=lambda row: abs(row.log2_ratio))

    st.markdown(
        '<span id="readout-3"></span><p class="section-kicker">Readout 3 · Exploratory dosage QC</p>',
        unsafe_allow_html=True,
    )
    _render_development_result_banner(st)
    st.subheader("Result 3 — Exploratory whole-chromosome relative dosage QC")
    st.caption(
        "Primary read starts → 5 Mb autosomal bins → low-coverage bin exclusion "
        "→ sample-internal chromosome medians. Uncalibrated development QC only."
    )
    accepted, bins, spread, events = st.columns(4)
    accepted.metric(
        "Accepted reads",
        f"{bundle.provenance.accepted_read_count:,}",
    )
    bins.metric("Window size", "5 Mb")
    spread.metric("Genome log₂ MAD", f"{bundle.genome_log2_mad:.3f}")
    events.metric(
        "Chromosomes outside visualization boundary",
        str(bundle.flagged_chromosome_count),
    )

    explanation, chart = st.columns((0.72, 1.28))
    with explanation:
        st.markdown("**How the chromosome-dosage algorithm works**")
        st.markdown(
            """
1. **Keep high-confidence reads**: primary, mapped, QC-pass, non-duplicate, MAPQ ≥20.
2. **Count read starts** in complete 5 Mb bins across chromosomes 1–22.
3. **Remove obvious coverage gaps** below 55% of each chromosome's median.
4. **Compare chromosome medians** with the genome-wide median and label values outside the unvalidated ±0.20 log₂ visualization boundary.
"""
        )
        st.markdown(
            """
<div class="interpretation-card">
  <strong>Why the scope is narrow</strong>
  <p>This shows sample-internal relative dosage values. Without calibration and a compatible normal panel, it cannot establish broad copy gains or losses.</p>
</div>
""",
            unsafe_allow_html=True,
        )
    with chart:
        st.markdown("**Autosomal dosage profile**")
        st.vega_lite_chart(
            rows,
            spec={
                "layer": [
                    {
                        "mark": {"type": "rule", "color": "#6B6B7B"},
                        "encoding": {"y": {"datum": 0}},
                    },
                    {
                        "mark": {
                            "type": "rule",
                            "color": "#6B6B7B",
                            "strokeDash": [5, 4],
                        },
                        "encoding": {"y": {"datum": EVENT_THRESHOLD_LOG2}},
                    },
                    {
                        "mark": {
                            "type": "rule",
                            "color": "#6B6B7B",
                            "strokeDash": [5, 4],
                        },
                        "encoding": {"y": {"datum": -EVENT_THRESHOLD_LOG2}},
                    },
                    {
                        "mark": {
                            "type": "bar",
                            "cornerRadiusTopLeft": 3,
                            "cornerRadiusTopRight": 3,
                        },
                        "encoding": {
                            "x": {
                                "field": "label",
                                "type": "ordinal",
                                "sort": [str(index) for index in range(1, 23)],
                                "title": "Chromosome",
                            },
                            "y": {
                                "field": "log2_ratio",
                                "type": "quantitative",
                                "title": "Chromosome median log₂ ratio",
                                "scale": {"domain": [-0.25, 0.25]},
                            },
                            "color": {
                                "condition": {
                                    "test": "datum.classification !== 'within_threshold'",
                                    "value": "#6B6B7B",
                                },
                                "value": "#1B7F79",
                            },
                            "tooltip": [
                                {"field": "chromosome", "title": "Chromosome"},
                                {
                                    "field": "log2_ratio",
                                    "title": "log₂ ratio",
                                    "format": "+.3f",
                                },
                                {
                                    "field": "estimated_copy_number",
                                    "title": "Illustrative relative dosage",
                                    "format": ".2f",
                                },
                                {
                                    "field": "retained_bin_count",
                                    "title": "Retained bins",
                                },
                            ],
                        },
                    },
                ],
                "config": {
                    "axis": {
                        "domainColor": "#C9C9D6",
                        "gridColor": "#E3E3E8",
                        "labelColor": "#6B6B7B",
                        "titleColor": "#1B1B2B",
                    },
                    "view": {"stroke": None},
                },
                "background": "#FFFFFF",
            },
            use_container_width=True,
            theme=None,
        )
        st.caption(
            "Dashed lines = prespecified, unvalidated ±0.20 log₂ visualization boundary."
        )

    boundary_summary = (
        "All autosomal medians remain inside the prespecified visualization boundary"
        if bundle.flagged_chromosome_count == 0
        else (
            f"{bundle.flagged_chromosome_count} autosomal median(s) fall outside "
            "the prespecified visualization boundary"
        )
    )
    st.markdown(
        f"""
<div class="interpretation-card">
  <div class="card-label">How to read the result</div>
  <strong>{boundary_summary}</strong>
  <p>
    The largest residual is {largest.chromosome} at {largest.log2_ratio:+.3f}
    log₂. Without a panel of normals, residual chromosome-specific coverage bias
    cannot be separated from biology; the algorithm therefore makes no focal,
    subclonal, or tumor-fraction claim.
  </p>
</div>
""",
        unsafe_allow_html=True,
    )
    st.caption(
        "This is not ichorCNA. It is uncalibrated, development-unqualified relative "
        "dosage QC computed from the registered BAM, not a diagnostic result."
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
            st.caption(f"{_source_heading(source)} · `{source.id}`")
            st.info(source.quote)
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
        claim.id: f"{_humanize(claim.kind)} · {claim.id}" for claim in state.case.claims
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


def _render_review_workspace(
    st: Any,
    state: UIState,
    runner: AuditRunner,
    *,
    replay_mode: bool,
) -> None:
    """Render the primary judge interaction before the long-form results."""

    st.markdown(
        """
<span id="ai-review"></span>
<div class="judge-moment">
  <p class="section-kicker">The judge moment · AI evidence review</p>
  <h3>Can the written method reproduce the chart?</h3>
  <p>
    The report says its updated method uses aligned reference span. Its
    reproduction instructions still say to subtract a fixed 45 bp. Run the
    review to test whether those passages actually agree.
  </p>
</div>
""",
        unsafe_allow_html=True,
    )
    if replay_mode:
        st.info(
            "Public demo mode replays a recorded, validated AI assessment and "
            "makes no provider call. The cited passages and computed evidence "
            "are the registered demo inputs."
        )
    else:
        st.caption(
            "Live mode: one explicit click sends bounded registered evidence to "
            "the configured Bedrock model."
        )
    pressed = st.button(
        "Show the mismatch" if replay_mode else "Run AI evidence review",
        type="primary",
        disabled=state.status == SessionStatus.RUNNING,
        help="Starts one review. Other interactions do not call the model.",
    )
    if pressed:
        with st.spinner("Checking both method passages…"):
            execute_audit(state, runner)

    if state.error is not None:
        _render_error(st, state.error)
    if state.selected_audit is not None:
        st.divider()
        _render_audit(st, state, state.selected_audit)
    else:
        st.caption("One click reveals the assessment and both exact citations.")
    with st.expander("Inspect the claim or choose another check", expanded=False):
        _render_claim_controls(st, state)


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
    st.set_page_config(page_title="Traceback", page_icon="🔎", layout="wide")
    _inject_design_system(st)
    if case is not None:
        active_case = case
    else:
        try:
            active_case = load_default_case()
        except CaseBundleLoadError:
            _render_header(st)
            st.error("INVALID_INPUT: The configured case bundle could not be loaded.")
            st.caption(
                "Verify that the configured bundle exists and satisfies the case "
                "contract. No fallback case was loaded."
            )
            return

    state = ensure_case(st.session_state.get(SESSION_KEY), active_case)
    st.session_state[SESSION_KEY] = state
    replay_mode = (
        audit_runner is None and os.environ.get("TRACEBACK_DEMO_REPLAY") == "1"
    )
    if replay_mode:
        from evidence_inspector.demo_replay import replay_assessment

        runner = replay_assessment
    else:
        runner = audit_runner or _default_runner

    _render_header(st)
    _render_truth_strip(st, replay_mode=replay_mode)
    _render_review_workspace(st, state, runner, replay_mode=replay_mode)
    _render_signal_overview(st)
    _render_case_scope(st, active_case)
    _render_fragmentomics_evidence(st, active_case)
    _render_cell_origin_evidence(st)
    _render_copy_number_evidence(st)


if __name__ == "__main__":
    run_app()
