"""Cell origin as a signed local record (signal methods CO3).

``run BAM --reference REF --analysis cell-origin --modbase-model ID`` runs one
job of three stages over the sealed BAM copy:

1. validate: the shared BAM preflight, then the cell-origin checks
   (TBX-METH-001 modification tags, TBX-METH-002 basecall model declaration,
   TBX-METH-003 reference contigs), after the three registered Loyfer assets
   are copied into the job and hashed there (SH2);
2. measure: the alignment pre-filter and the bounded modkit extract (CO1),
   UXM classification, NNLS and the seeded bootstrap, refused on a cap hit
   (TBX-METH-005), below the floors (TBX-METH-004), when the solver does not
   converge (TBX-METH-006) or when a validation check fails (TBX-METH-007);
3. sign: a ``result-bundle.v4`` record whose method identity is the locked
   ``mth_cell_origin_loyfer_uxm`` definition (CO2).

The measurement is :class:`CellOriginMeasurementV1`, registered for v4 at
``measurements/cell-origin.v1.json`` and ``charts/cell-origin.v1.json``
(SH3).  Every record is unqualified, local, not for clinical use and
descriptive only: fractions among registered atlas contributors, forced to
sum to 1, never compared with any reference range.

Importing this module registers the measurement schema and the analysis
stages; ``traceback_runner.cli`` imports it.

Threat model: the OS-user boundary.  In-process code mutation and same-user
filesystem races are out of scope.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from .analyses import CELL_ORIGIN, AnalysisStages, ReadinessRow, register_analysis_stages
from .cell_origin_method import (
    METHOD_SLUG,
    CellOriginParametersV1,
    cell_origin_method_definition,
    default_parameters,
    registered_loyfer_assets,
)
from .contracts import ApprovalState, RunnerContract
from .export import LOCAL_REPORT_BANNER, ReferenceMatch
from .measurement_schemas import (
    BundleMeasurementSchema,
    LocalCatalogBinding,
    register_measurement_schema,
)
from .references import LOYFER_DIRECTORY_FILES, AssetKind
from .serialization import canonical_json_bytes

SCHEMA_VERSION = "traceback.cell-origin-measurement.v1"
CHART_SCHEMA_VERSION = "traceback.cell-origin-chart.v1"
LIMITATIONS_SCHEMA_VERSION = "traceback.cell-origin-limitations.v1"
LIMITATIONS_TEMPLATE = "local-cell-origin-research-use.v1"
PATH_STEM = "cell-origin.v1"
DEFINITION_ID_PREFIX = "cell-origin-loyfer-uxm-v1"
STAGE_VERSION = "1"
PREFLIGHT_OUTPUT = "cell-origin-preflight.json"
MEASUREMENT_OUTPUT = "measurement.json"
TOOL_OUTPUT = "tool.json"
TOOL_ROLE = "tool_binary"

# The fixed limitation statements (spec §3.1).  "Diagnosis" and "healthy"
# stay out: every record text passes the export claim check.
LIMITATION_STATEMENTS: tuple[str, ...] = (
    "Estimated fraction among registered atlas contributors (Loyfer U250 atlas). "
    "Fractions are forced to sum to 1; the method has no unassigned compartment.",
    "Descriptive only; not a medical finding and not compared with any reference range.",
    "Unqualified, local, not for clinical use.",
    "ONT basecaller methylation calls; the atlas was built from WGBS.",
)

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")]
Count = Annotated[int, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class ModbaseModel(RunnerContract):
    """The modified-base model, and where its ID came from."""

    id: ModelId
    source: Literal["header", "operator_declared"]


class AlignmentExclusions(RunnerContract):
    """Alignments the pre-filter excluded, by the fragment policy's reasons."""

    unmapped: Count
    secondary: Count
    supplementary: Count
    qc_failure: Count
    duplicate: Count
    low_mapping_quality: Count

    @property
    def total(self) -> int:
        return (
            self.unmapped
            + self.secondary
            + self.supplementary
            + self.qc_failure
            + self.duplicate
            + self.low_mapping_quality
        )


class CellOriginDenominators(RunnerContract):
    """Every count a reader needs to judge how much data a mixture rests on.

    Fragment counts are fragment-marker observations: a fragment overlapping
    two markers counts once at each.  ``registered_markers`` counts the atlas
    markers the fit can use (complete atlas rows); markers with an NA value
    are counted in ``atlas_markers_excluded_incomplete``.
    """

    records_scanned: Count
    alignment_exclusions: AlignmentExclusions
    eligible_alignments: Count
    alignments_with_mod_tags: Count
    marker_overlapping_fragments: Count
    classified_fragments: Count  # U + M
    mixed_fragments: Count  # X
    excluded_fewer_than_4_cpgs: Count
    registered_markers: Annotated[int, Field(ge=1)]
    atlas_markers_excluded_incomplete: Count
    observed_markers: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def reconcile(self) -> CellOriginDenominators:
        if self.records_scanned - self.alignment_exclusions.total != self.eligible_alignments:
            raise ValueError("eligible alignments must be the scanned minus the excluded")
        if self.alignments_with_mod_tags > self.eligible_alignments:
            raise ValueError("modification-tagged alignments must be eligible alignments")
        if self.observed_markers > self.registered_markers:
            raise ValueError("observed markers cannot exceed registered markers")
        return self


class CpgCallExclusions(RunnerContract):
    """CpG calls (or groups) set aside before UXM classification (uxm.py)."""

    duplicate_calls: Count
    failed_calls: Count
    non_c_calls: Count
    unsupported_modification_calls: Count
    invalid_calls: Count
    outside_marker_calls: Count
    ambiguous_marker_calls: Count
    unknown_marker_calls: Count
    conflicting_duplicate_groups: Count
    oversized_groups: Count


class CpgCalls(RunnerContract):
    """CpG calls from modkit's extract to the UXM classifier.

    ``extracted`` counts the extract's rows; ``dropped_at_load`` the rows the
    call loader set aside (failed, non-C or malformed rows) before the
    ``inspected`` calls reached classification.
    """

    extracted: Count
    dropped_at_load: Count
    inspected: Count
    retained: Count
    excluded_by_reason: CpgCallExclusions

    @model_validator(mode="after")
    def bounded(self) -> CpgCalls:
        if self.extracted != self.dropped_at_load + self.inspected:
            raise ValueError("extracted calls must be dropped or inspected")
        if self.retained > self.inspected:
            raise ValueError("retained CpG calls cannot exceed inspected calls")
        return self


class MarkerCount(RunnerContract):
    """Integer U/M/X fragment counts at one marker: the solver's input."""

    marker_id: Identifier
    u: Count
    m: Count
    x: Count


class FractionInterval(RunnerContract):
    low: Fraction
    high: Fraction


class ContributorEstimate(RunnerContract):
    contributor_id: Identifier
    fraction: Fraction
    raw_nnls_weight: NonNegative
    interval: FractionInterval | None
    interval_state: Literal["available", "insufficient_information"]


class SolverDiagnostics(RunnerContract):
    """A record exists only for a converged fit (TBX-METH-006 otherwise)."""

    converged: Literal[True]
    iterations: Count
    residual_l2: NonNegative
    objective_value: NonNegative
    row_scale: Literal["sqrt_count", "reference_count", "unweighted"]


class CellOriginMeasurementV1(RunnerContract):
    """One BAM's estimated mixture among registered atlas contributors.

    Validators reuse the evidence models (``cell_origin_models``): each marker
    row is a ``MarkerCountRow``, each interval a ``BootstrapIntervalV2`` and the
    estimates a ``DeconvolutionOutput`` (unique contributors, fractions sum
    to 1).  ``classified_fragments`` is the sum of U + M over the markers.
    """

    schema_version: Literal["traceback.cell-origin-measurement.v1"] = SCHEMA_VERSION
    definition_id: Identifier
    approval_state: Literal[ApprovalState.UNAPPROVED_LOCAL]
    reference_id: Identifier
    modbase_model: ModbaseModel
    denominators: CellOriginDenominators
    cpg_calls: CpgCalls
    marker_counts: tuple[MarkerCount, ...] = Field(min_length=1)
    estimates: tuple[ContributorEstimate, ...] = Field(min_length=1)
    solver: SolverDiagnostics

    @model_validator(mode="after")
    def reuse_evidence_validators(self) -> CellOriginMeasurementV1:
        from evidence_inspector.cell_origin_models import (
            LOYFER_UXM_METHOD,
            BootstrapInformationStatus,
            BootstrapIntervalV2,
            CellFractionEstimate,
            DeconvolutionOutput,
            MarkerCountRow,
            NnlsDiagnostics,
        )

        if self.definition_id != f"{DEFINITION_ID_PREFIX}.{self.reference_id}":
            raise ValueError("definition_id must name the reference")
        markers = [row.marker_id for row in self.marker_counts]
        if len(set(markers)) != len(markers):
            raise ValueError("marker count rows must be unique")
        for row in self.marker_counts:
            total = row.u + row.x + row.m
            if total < 1:
                raise ValueError("a counted marker has at least one classified fragment")
            MarkerCountRow(
                marker_id=row.marker_id,
                u_count=row.u,
                x_count=row.x,
                m_count=row.m,
                classified_fragment_count=total,
                u_fraction=row.u / total,
            )
        denominators = self.denominators
        if denominators.classified_fragments != sum(row.u + row.m for row in self.marker_counts):
            raise ValueError("classified_fragments must equal the sum of U + M over markers")
        if denominators.mixed_fragments != sum(row.x for row in self.marker_counts):
            raise ValueError("mixed_fragments must equal the sum of X over markers")
        if denominators.observed_markers != len(self.marker_counts):
            raise ValueError("observed_markers must equal the counted markers")
        # Each classified or set-aside fragment-marker group came from a group
        # of retained calls on an eligible, modification-tagged alignment.
        if (
            denominators.classified_fragments
            + denominators.mixed_fragments
            + denominators.excluded_fewer_than_4_cpgs
            > denominators.marker_overlapping_fragments
        ):
            raise ValueError("classified groups cannot exceed marker-overlapping fragments")
        if denominators.marker_overlapping_fragments > self.cpg_calls.retained:
            raise ValueError("every marker-overlapping fragment has a retained CpG call")
        if denominators.alignments_with_mod_tags < 1:
            raise ValueError("a mixture needs modification-tagged alignments")
        contributors = [item.contributor_id for item in self.estimates]
        if contributors != sorted(contributors):
            raise ValueError("estimates are sorted by contributor_id")
        DeconvolutionOutput(
            result_id="cell-origin.measurement",
            method=LOYFER_UXM_METHOD,
            atlas_id="atlas.registered",
            marker_ids=tuple(markers),
            estimates=tuple(
                CellFractionEstimate(
                    cell_type_id=item.contributor_id,
                    raw_nnls_weight=item.raw_nnls_weight,
                    fraction=item.fraction,
                )
                for item in self.estimates
            ),
            diagnostics=NnlsDiagnostics(
                converged=self.solver.converged,
                iterations=self.solver.iterations,
                residual_l2=self.solver.residual_l2,
                objective_value=self.solver.objective_value,
            ),
        )
        for item in self.estimates:
            available = item.interval_state == "available"
            BootstrapIntervalV2(
                cell_type_id=item.contributor_id,
                estimate=item.fraction,
                information_status=(
                    BootstrapInformationStatus.AVAILABLE
                    if available
                    else BootstrapInformationStatus.INSUFFICIENT_INFORMATION
                ),
                lower_fraction=item.interval.low if item.interval is not None else None,
                upper_fraction=item.interval.high if item.interval is not None else None,
            )
        return self


class CellOriginChartRow(RunnerContract):
    rank: Annotated[int, Field(ge=1)]
    contributor_id: Identifier
    fraction: Fraction
    interval: FractionInterval | None


class CellOriginChartV1(RunnerContract):
    """Ranked contributor rows derived losslessly from the measurement."""

    schema_version: Literal["traceback.cell-origin-chart.v1"] = CHART_SCHEMA_VERSION
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: tuple[CellOriginChartRow, ...] = Field(min_length=1)
    classified_fragments: Count
    observed_markers: Count
    registered_markers: Count
    residual_l2: NonNegative


class CellOriginLimitationsV1(RunnerContract):
    schema_version: Literal["traceback.cell-origin-limitations.v1"] = LIMITATIONS_SCHEMA_VERSION
    template_id: Literal["local-cell-origin-research-use.v1"] = LIMITATIONS_TEMPLATE
    reference_match: ReferenceMatch
    statements: tuple[str, ...] = LIMITATION_STATEMENTS

    @model_validator(mode="after")
    def fixed_statements(self) -> CellOriginLimitationsV1:
        if self.statements != LIMITATION_STATEMENTS:
            raise ValueError("cell-origin limitations are the fixed statements")
        return self


# ---------------------------------------------------------------------------
# Chart, limitations, report, catalog denominator (pure functions)
# ---------------------------------------------------------------------------


def build_chart(measurement: CellOriginMeasurementV1, sha256: str) -> CellOriginChartV1:
    ranked = sorted(measurement.estimates, key=lambda item: (-item.fraction, item.contributor_id))
    return CellOriginChartV1(
        measurement_sha256=sha256,
        rows=tuple(
            CellOriginChartRow(
                rank=rank,
                contributor_id=item.contributor_id,
                fraction=item.fraction,
                interval=item.interval,
            )
            for rank, item in enumerate(ranked, start=1)
        ),
        classified_fragments=measurement.denominators.classified_fragments,
        observed_markers=measurement.denominators.observed_markers,
        registered_markers=measurement.denominators.registered_markers,
        residual_l2=measurement.solver.residual_l2,
    )


def build_limitations(
    measurement: CellOriginMeasurementV1, reference_match: ReferenceMatch
) -> CellOriginLimitationsV1:
    return CellOriginLimitationsV1(reference_match=reference_match)


def _model_source(model: ModbaseModel) -> str:
    return (
        "declared by the operator, not read from the file"
        if model.source == "operator_declared"
        else "declared in the BAM header"
    )


def render_report(
    measurement: CellOriginMeasurementV1, limitations: CellOriginLimitationsV1
) -> bytes:
    """The minimal local report: identity, basis and limitations.

    No fraction is printed here (gate G1); the fractions are in the signed
    measurement file.
    """

    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    denominators = measurement.denominators
    reference_item = (
        "<li>The reference was matched by contig name and length only; the input "
        "header carried no sequence digests to compare.</li>"
        if limitations.reference_match == "name_and_length_only"
        else ""
    )
    statements = "".join(f"<li>{escape(item)}</li>" for item in limitations.statements)
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback local cell-origin record</title>"
        f"<p role=\"note\"><strong>{escape(LOCAL_REPORT_BANNER)}</strong></p>"
        "<h1>Local cell-origin research record</h1>"
        f"<p>Definition: <code>{escape(measurement.definition_id)}</code>; reference: "
        f"<code>{escape(measurement.reference_id)}</code>.</p>"
        f"<p>Based on {denominators.classified_fragments} classified fragments at "
        f"{denominators.observed_markers} of {denominators.registered_markers} atlas "
        f"markers; {denominators.eligible_alignments} eligible alignments of "
        f"{denominators.records_scanned} records scanned.</p>"
        f"<p>Basecall model: <code>{escape(measurement.modbase_model.id)}</code>, "
        f"{escape(_model_source(measurement.modbase_model))}.</p>"
        "<p>The estimated fractions are in the signed measurement file of this "
        "record.</p>"
        "<h2>Limitations</h2><ul>"
        "<li>The measurement method is unqualified for this input.</li>"
        "<li>The record is signed with a development key only; it carries no "
        "production trust.</li>"
        f"{reference_item}{statements}</ul></html>"
    )
    return body.encode("utf-8")


def _denominator_ledger(verified: Any) -> Any:
    """The E06 ledger in alignment units: scanned, accepted, eligible."""

    from evidence_inspector.result_view import (
        AttritionReason,
        AttritionStage,
        CountState,
        CountValue,
        DenominatorLedger,
    )

    denominators = verified.measurement.denominators
    excluded = denominators.alignment_exclusions

    def observed(value: int, label: str) -> CountValue:
        return CountValue(state=CountState.OBSERVED, value=value, accessible_label=label)

    def reason(stage: AttritionStage, code: str, value: int, label: str) -> AttritionReason:
        return AttritionReason(
            stage=stage,
            reason_code=f"reason_{code}",
            accessible_label=label,
            count=observed(value, label),
        )

    acceptance = (
        ("duplicate", excluded.duplicate, "Duplicate alignments"),
        ("qc_failure", excluded.qc_failure, "QC-failed alignments"),
        ("secondary", excluded.secondary, "Secondary alignments"),
        ("supplementary", excluded.supplementary, "Supplementary alignments"),
        ("unmapped", excluded.unmapped, "Unmapped records"),
    )
    accepted = denominators.records_scanned - sum(item[1] for item in acceptance)
    attrition = tuple(
        sorted(
            (
                *(reason(AttritionStage.ACCEPTANCE, *item) for item in acceptance),
                reason(
                    AttritionStage.ELIGIBILITY,
                    "low_mapping_quality",
                    excluded.low_mapping_quality,
                    "Below MAPQ 20",
                ),
                reason(AttritionStage.DISPLAY, "none_withheld", 0, "None withheld"),
            ),
            key=lambda item: item.sort_key,
        )
    )
    return DenominatorLedger(
        input_records=observed(denominators.records_scanned, "Records scanned"),
        accepted_records=observed(accepted, "Primary mapped alignments"),
        eligible_records=observed(denominators.eligible_alignments, "Eligible alignments"),
        displayed_records=observed(denominators.eligible_alignments, "Displayed alignments"),
        attrition=attrition,
    )


def _no_reference_store(root: Any, registered: Any) -> Any:
    raise ValueError("cell-origin records bind their own hash-keyed method store")


MEASUREMENT_SCHEMA = BundleMeasurementSchema(
    schema_version=SCHEMA_VERSION,
    path_stem=PATH_STEM,
    measurement_model=CellOriginMeasurementV1,
    chart_model=CellOriginChartV1,
    limitations_model=CellOriginLimitationsV1,
    build_chart=build_chart,
    build_limitations=build_limitations,
    render_report=render_report,
    catalog=LocalCatalogBinding(
        result_schema_id="schema_cell_origin_measurement",
        result_schema_version="1.0.0",
        accessible_label="Cell origin, unqualified local record",
        normalization_semantics_id="sem_cell_origin_atlas_fraction",
        coordinate_semantics_id="sem_loyfer_marker_regions",
        denominator_semantics_id="sem_eligible_alignments",
        authority=_no_reference_store,
        denominator=_denominator_ledger,
        method_slug=METHOD_SLUG,
        # The explorer's E05 key binds the exact registered atlas (signal CO4).
        asset_roles=(("atlas_asset", LOYFER_DIRECTORY_FILES[AssetKind.LOYFER_ATLAS][1]),),
    ),
)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _refusal(code: str, summary: str, *, cause: str, fix: str) -> Exception:
    from .cli import LocalStageRefusal

    return LocalStageRefusal(code, summary + "; no record was made", cause=cause, fix=fix)


def _retryable(code: str, summary: str, *, cause: str, fix: str) -> Exception:
    """A stage problem a retry can fix (a moved file): the job can resume."""

    from .cli import ToolUnavailableAtStage
    from .references import ReferenceProblem

    return ToolUnavailableAtStage(ReferenceProblem(code, summary, cause=cause, fix=fix))


def _asset_problem(problem: Any) -> Exception:
    """An asset registration or copy problem at stage time stays retryable."""

    from .cli import ToolUnavailableAtStage

    return ToolUnavailableAtStage(problem)


def _region_contigs(regions: Sequence[str]) -> set[str]:
    return {region.rsplit(":", 1)[0] for region in regions}


def _header_models(bam: Path) -> tuple[str, ...]:
    """Every ``modbase_models=`` ID the sealed BAM's ``@RG DS`` lines declare."""

    import pysam

    from .preflight import _declared_modbase_models

    with pysam.AlignmentFile(str(bam), "rb", check_sq=False) as reader:
        header = reader.header.to_dict()
    models: list[str] = []
    for group in header.get("RG", []):
        if isinstance(group, dict):
            models.extend(_declared_modbase_models(group.get("DS")))
    return tuple(dict.fromkeys(models))


def _model(model_id: str, source: str) -> ModbaseModel:
    from pydantic import ValidationError

    try:
        return ModbaseModel(id=model_id, source=source)
    except ValidationError:
        raise _refusal(
            "TBX-METH-002",
            "The declared basecall model is not a plain model ID",
            cause="the model ID holds characters other than letters, digits and . _ @ + -",
            fix="Pass the model ID as run --modbase-model ID",
        ) from None


def modbase_readiness(bam: Path, declared: str | None) -> ReadinessRow:
    """``preflight --analysis cell-origin``: the stage's own tag and model checks.

    The same sampling and resolution as the validate stage, so the row and the
    job agree: TBX-METH-001 (tags), the TBX-MOD-001 row naming the flag (no
    model), TBX-METH-002 (a contradicting or unusable declaration).
    """

    refusal = modification_refusal(*sample_modification_tags(bam))
    if refusal is not None:
        return ReadinessRow("TBX-METH-001", "blocked", refusal.cause)  # type: ignore[attr-defined]
    header_models = _header_models(bam)
    if declared is None and not header_models:
        return ReadinessRow(
            "TBX-MOD-001", "blocked", "BLOCKED for cell origin: pass `--modbase-model`"
        )
    try:
        model = resolve_modbase_model(header_models, declared)
    except Exception as exc:  # LocalStageRefusal
        return ReadinessRow("TBX-METH-002", "blocked", getattr(exc, "cause", str(exc)))
    return ReadinessRow(
        "TBX-MOD-001", "ready", f"modified-base model {_model_source(model)}"
    )


def resolve_modbase_model(header_models: Sequence[str], declared: str | None) -> ModbaseModel:
    """The record's model and its source, or TBX-METH-002.

    A declaration that contradicts the header is refused; a header that
    declares one model needs no declaration.
    """

    fix = (
        "Copy the model from the unaligned BAM's @RG line (samtools view -H "
        "UNALIGNED.bam | grep '^@RG') and pass it as run --modbase-model ID"
    )
    if len(header_models) > 1:
        raise _refusal(
            "TBX-METH-002",
            "The BAM header declares more than one modified-base model",
            cause="the @RG lines declare different modbase_models; one record names one model",
            fix="Split the BAM by read group and run each part",
        )
    if declared is not None:
        if header_models and declared not in header_models:
            raise _refusal(
                "TBX-METH-002",
                "The declared basecall model contradicts the BAM header",
                cause="--modbase-model names a model the @RG lines do not declare",
                fix="Pass the model the BAM header declares, or none",
            )
        return _model(declared, "header" if declared in header_models else "operator_declared")
    if len(header_models) == 1:
        return _model(header_models[0], "header")
    raise _refusal(
        "TBX-METH-002",
        "No basecall model is declared for the modification calls",
        cause=(
            "the BAM header declares no modified-base model (alignment drops @RG) and "
            "run was not given --modbase-model"
        ),
        fix=fix,
    )


MODIFICATION_SAMPLE = 100


def sample_modification_tags(bam: Path, *, records: int = MODIFICATION_SAMPLE) -> tuple[int, int]:
    """``(tagged, invalid)`` over the first mapped primary records of ``bam``.

    A record is tagged when it carries MM (or Mm); it is invalid when its
    MM/ML do not parse to one call per ML value, or when an optional MN
    differs from the query length.  MN is optional (spec §3.1).
    """

    import pysam

    tagged = invalid = seen = 0
    with pysam.AlignmentFile(str(bam), "rb", check_sq=False) as reader:
        for record in reader.fetch(until_eof=True):
            if record.is_unmapped or record.is_secondary or record.is_supplementary:
                continue
            seen += 1
            if seen > records:
                break
            if not (record.has_tag("MM") or record.has_tag("Mm")):
                continue
            tagged += 1
            try:
                ml = record.get_tag("ML") if record.has_tag("ML") else record.get_tag("Ml")
                calls = sum(len(items) for items in record.modified_bases.values())
                mn_ok = (
                    not record.has_tag("MN") or record.get_tag("MN") == record.query_length
                )
                valid = calls == len(ml) and mn_ok
            except (KeyError, TypeError, ValueError, AttributeError):
                valid = False
            invalid += 0 if valid else 1
    return tagged, invalid


def modification_refusal(tagged: int, invalid: int) -> Exception | None:
    """TBX-METH-001 when the sampled MM/ML tags are absent or contradictory."""

    if invalid:
        cause = "the sampled MM/ML modification tags contradict each other"
    elif not tagged:
        cause = "the sampled reads carry no MM/ML modification tags"
    else:
        return None
    return _refusal(
        "TBX-METH-001",
        "Cell origin needs modification calls this BAM does not carry",
        cause=cause,
        fix=(
            "Basecall with a modified-base model (5mC), keep MM/ML through alignment "
            "(samtools fastq -T MM,ML,MN), and run again; the fragment analysis is "
            "unaffected"
        ),
    )


def contig_refusal(region_contigs: set[str], reference: Any) -> Exception | None:
    """TBX-METH-003 when a marker-region contig is not a registered contig."""

    registered = {contig.name for contig in reference.contigs}
    missing = sorted(region_contigs - registered)
    if not missing:
        return None
    shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    return _refusal(
        "TBX-METH-003",
        "The registered reference lacks contigs the Loyfer marker regions use",
        cause=f"marker-region contigs not in the registered reference: {shown}",
        fix=(
            "Register and align against the hg38 FASTA the atlas uses (UCSC chr names), "
            "then run again"
        ),
    )


class CellOriginAnalysis:
    """The cell-origin method's SH4 stages, parameters and tool resolver.

    ``parameters(modbase_model)`` returns the locked parameters (tests pass
    other floors); ``modkit()`` returns the verified pinned modkit.
    """

    def __init__(
        self,
        *,
        parameters: Callable[..., CellOriginParametersV1] = default_parameters,
        modkit: Callable[[], Any] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.parameters = parameters
        self.modkit = modkit
        self.platform_name = platform_name
        self.spec = AnalysisStages(
            analysis=CELL_ORIGIN,
            method_slug=METHOD_SLUG,
            definition=self.definition,
            stages=self.stages,
            config_keys=frozenset({"modbase_model"}),
            readiness=self.readiness,
            takes_root=True,
        )

    # -- method ------------------------------------------------------------

    def locked_parameters(self, config: Mapping[str, str]) -> CellOriginParametersV1:
        return self.parameters(modbase_model=config.get("modbase_model"))

    def definition(self, loaded: Any, config: Mapping[str, str], *, root: Path) -> Any:
        from .toolchain import pin_for

        return cell_origin_method_definition(
            loaded.registered,
            registered_loyfer_assets(root),
            pin_for("modkit", self.platform_name),
            self.locked_parameters(config),
        )

    def readiness(
        self, loaded: Any, config: Mapping[str, str], *, root: Path
    ) -> list[ReadinessRow]:
        """Asset registration (TBX-ASSET-004) and reference contigs (TBX-METH-003)."""

        from evidence_inspector.cell_origin_pipeline import (
            CellOriginPipelineError,
            read_marker_regions,
        )

        from .references import ReferenceProblem, load_asset

        rows: list[ReadinessRow] = []
        regions_path: Path | None = None
        for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items():
            try:
                loaded_asset = load_asset(root, asset_id, kind=kind)
            except ReferenceProblem:
                rows.append(
                    ReadinessRow(
                        "TBX-ASSET-004",
                        "not_set_up",
                        f"{kind.value} not registered; next: traceback method-asset "
                        "register --from-dir LOYFER_DIR --root <same-root>",
                    )
                )
                continue
            if kind is AssetKind.LOYFER_REGIONS:
                regions_path = Path(loaded_asset.source.file_path)
        if regions_path is not None:
            try:
                contigs = _region_contigs(read_marker_regions(regions_path))
            except (CellOriginPipelineError, OSError):
                rows.append(
                    ReadinessRow(
                        "TBX-ASSET-003", "blocked", "the registered regions file is unreadable"
                    )
                )
            else:
                refusal = contig_refusal(contigs, loaded.registered)
                rows.append(
                    ReadinessRow("TBX-METH-003", "ready", "marker-region contigs registered")
                    if refusal is None
                    else ReadinessRow("TBX-METH-003", "blocked", refusal.cause)  # type: ignore[attr-defined]
                )
        return rows

    # -- stages -----------------------------------------------------------

    def stages(self, context: Any) -> tuple[Any, ...]:
        from evidence_inspector.method_registry import method_definition_sha256

        from .contracts import StageName
        from .runner import StageSpec

        root = context.root
        loaded = context.loaded
        parameters = self.locked_parameters(context.config)
        definition = self.definition(loaded, context.config, root=root)
        definition_sha256 = method_definition_sha256(definition)
        common = {"definition_sha256": definition_sha256, "data_origin": "local_unqualified"}
        return (
            StageSpec(
                name=StageName.VALIDATE,
                version=STAGE_VERSION,
                callback=lambda stage: self._validate(stage, context, definition),
                parameters=common,
            ),
            StageSpec(
                name=StageName.MEASURE,
                version=STAGE_VERSION,
                callback=lambda stage: self._measure(stage, context, parameters),
                parameters=common,
            ),
            StageSpec(
                name=StageName.SIGN,
                version=STAGE_VERSION,
                callback=lambda stage: self._sign(stage, context, definition),
                parameters={**common, "key_id": context.signing_key.key_id},
            ),
        )

    def _validate(self, stage: Any, context: Any, definition: Any) -> Any:
        from evidence_inspector.cell_origin_pipeline import (
            CellOriginPipelineError,
            read_marker_regions,
        )

        from .cli import _LOCAL_PREFLIGHT_POLICY, StaleLease, TerminalStageError
        from .contracts import PreflightOutcome
        from .preflight import BamPreflightPolicy, validate_bam_snapshot
        from .references import ReferenceProblem, copy_registered_asset, load_asset
        from .runner import StageResult

        registered = context.loaded.registered
        context.progress("STAGE  cell-origin preflight: inspecting the sealed BAM copy")
        bam = stage.sealed_input_dir / context.bam_name
        try:
            report = validate_bam_snapshot(
                bam,
                stage.sealed_input_dir / context.index_name,
                registered,
                BamPreflightPolicy(policy_id=_LOCAL_PREFLIGHT_POLICY),
                compare_assembly=context.loaded.source.assembly_declared,
            )
            header_models = _header_models(bam)
            tagged, invalid = sample_modification_tags(bam)
        except (StaleLease, TerminalStageError):
            raise
        except Exception as exc:
            raise _refusal(
                "TBX-INTERNAL-001",
                "Preflight stopped on an unexpected internal error",
                cause=f"{type(exc).__name__} while inspecting the sealed BAM copy",
                fix="Retrying will not change it; write `traceback support-bundle JOB_ID "
                "--output DIR` for this job and report the code",
            ) from exc
        blocked = [check for check in report.checks if check.outcome == PreflightOutcome.BLOCKED]
        if blocked or not report.fragment_measurement_eligible:
            codes = sorted({check.code for check in blocked})
            raise _refusal(
                "TBX-BAM-002" if "TBX-BAM-002" in codes else (codes or ["TBX-BAM-001"])[0],
                "Preflight blocked this BAM",
                cause="; ".join(check.problem for check in blocked)
                or "the input is not an eligible aligned BAM",
                fix="Run traceback preflight BAM --reference ID for each check's remediation",
            )
        refusal = modification_refusal(tagged, invalid)
        if refusal is not None:
            raise refusal
        model = resolve_modbase_model(header_models, context.config.get("modbase_model"))
        # The registered assets, copied into this job and hashed here; each copy
        # must be the bytes the job's method definition names.
        bound = {item.asset_id: item.content_sha256 for item in definition.assets}
        copies: dict[str, str] = {}
        for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items():
            try:
                registration = load_asset(context.root, asset_id, kind=kind).registered
                if bound.get(asset_id) != registration.file_sha256:
                    raise _refusal(
                        "TBX-JOB-003",
                        "An asset registration differs from the job's method",
                        cause=f"{asset_id} is not the asset this job's method definition names",
                        fix="Run the input again with traceback run; the current method is "
                        "a new job",
                    )
                copy = copy_registered_asset(
                    context.root, asset_id, stage.attempt_dir, kind=kind
                )
            except ReferenceProblem as problem:
                raise _asset_problem(problem) from problem
            copies[kind.value] = copy.name
        try:
            regions = read_marker_regions(stage.attempt_dir / copies["loyfer-regions"])
        except CellOriginPipelineError as exc:
            raise _refusal(
                "TBX-ASSET-005",
                "The job's copy of the marker regions does not parse",
                cause=str(exc),
                fix="Register the unmodified Loyfer regions file under a new ID",
            ) from exc
        refusal = contig_refusal(_region_contigs(regions), registered)
        if refusal is not None:
            raise refusal
        output = stage.attempt_dir / PREFLIGHT_OUTPUT
        output.write_bytes(
            canonical_json_bytes(
                {
                    "preflight": report.model_dump(mode="json"),
                    "modbase_model": model.model_dump(mode="json"),
                    "asset_copies": copies,
                }
            )
        )
        context.progress(
            f"STAGE  cell-origin preflight {report.outcome.value}: basecall model "
            f"{model.source.replace('_', ' ')}"
        )
        return StageResult(
            outputs={
                "cell_origin_preflight": output.name,
                **{f"asset_{kind.replace('-', '_')}": name for kind, name in copies.items()},
            },
            metadata={"preflight_outcome": report.outcome.value, "data_origin": "local_unqualified"},
        )

    def _measure(self, stage: Any, context: Any, parameters: CellOriginParametersV1) -> Any:
        from .runner import StageResult

        source = context.loaded.source
        fasta = Path(source.fasta_path)
        context.progress("STAGE  cell-origin measure: re-hashing the registered FASTA")
        try:
            size = fasta.stat().st_size
            digest = _sha256_file(fasta) if size == source.fasta_size_bytes else None
        except OSError:
            digest = None
        # modkit reads the FASTA by reference, so its bytes must still be the
        # registered ones (the method hash names them).
        if digest != context.loaded.registered.asset_sha256:
            # A moved or changed FASTA can be restored; the job can resume.
            raise _retryable(
                "TBX-REF-001",
                "The registered reference FASTA is missing or changed",
                cause="the registered FASTA is missing, or its bytes differ from the registration",
                fix="Restore the registered FASTA, then resume the job",
            )
        prior = json.loads((stage.prior_stage_dirs[0] / PREFLIGHT_OUTPUT).read_bytes())
        copies = {
            kind: stage.prior_stage_dirs[0] / name for kind, name in prior["asset_copies"].items()
        }
        tool = self.modkit() if self.modkit is not None else _resolve_modkit()
        context.progress(
            "STAGE  cell-origin measure: pre-filter, modkit extract, UXM, NNLS, bootstrap"
        )
        measurement = compute_measurement(
            parameters=parameters,
            reference_id=context.loaded.registered.reference_id,
            modbase_model=ModbaseModel.model_validate(prior["modbase_model"]),
            bam=stage.sealed_input_dir / context.bam_name,
            fasta=fasta,
            atlas=copies["loyfer-atlas"],
            markers=copies["loyfer-markers"],
            regions=copies["loyfer-regions"],
            job_directory=stage.attempt_dir,
            modkit=tool,
            heartbeat=getattr(stage, "heartbeat", None),
        )
        output = stage.attempt_dir / MEASUREMENT_OUTPUT
        output.write_bytes(canonical_json_bytes(measurement))
        tool_output = stage.attempt_dir / TOOL_OUTPUT
        tool_output.write_bytes(
            canonical_json_bytes(
                {
                    "identity": tool.identity.model_dump(mode="json"),
                    "size_bytes": tool.path.stat().st_size,
                }
            )
        )
        denominators = measurement.denominators
        context.progress(
            f"STAGE  cell-origin measure: {denominators.classified_fragments} classified "
            f"fragments at {denominators.observed_markers} of "
            f"{denominators.registered_markers} markers"
        )
        return StageResult(
            outputs={"cell_origin_measurement": output.name, "tool": tool_output.name},
            metadata={
                "classified_fragments": denominators.classified_fragments,
                "observed_markers": denominators.observed_markers,
                "data_origin": "local_unqualified",
            },
        )

    def _sign(self, stage: Any, context: Any, definition: Any) -> Any:
        from evidence_inspector.method_registry import method_definition_sha256

        from .bundles import build_result_bundle
        from .cli import _LOCAL_WORKFLOW_ID, _provenance_hmac_key
        from .contracts import (
            ArtifactCommitment,
            BundleMethodIdentity,
            ExportRunProvenance,
            InputKind,
            PreflightOutcome,
            PreflightReport,
        )
        from .runner import StageResult

        context.progress("STAGE  cell-origin sign: development-local key (development trust only)")
        prior = json.loads((stage.prior_stage_dirs[0] / PREFLIGHT_OUTPUT).read_bytes())
        report = PreflightReport.model_validate(prior["preflight"])
        measurement = CellOriginMeasurementV1.model_validate_json(
            (stage.prior_stage_dirs[-1] / MEASUREMENT_OUTPUT).read_bytes()
        )
        tool = json.loads((stage.prior_stage_dirs[-1] / TOOL_OUTPUT).read_bytes())
        manifest = json.loads(
            (stage.sealed_input_dir / "input-manifest.local.json").read_bytes()
        )
        sealed = {item["relative_path"]: item for item in manifest["files"]}
        key = _provenance_hmac_key(context.root)
        bam_hmac = hmac.new(
            key,
            b"traceback.provider-artifact.v1|"
            + bytes.fromhex(sealed[context.bam_name]["sha256_local"]),
            hashlib.sha256,
        ).hexdigest()
        # The installed binary's digest, committed like the BAM (per-ROOT key).
        tool_hmac = hmac.new(
            key,
            b"traceback.tool-binary.v1|"
            + bytes.fromhex(tool["identity"]["installed_binary_sha256"]),
            hashlib.sha256,
        ).hexdigest()
        provenance = ExportRunProvenance(
            run_token=f"local-run-{stage.job_id[:16]}",
            input_kind=InputKind.MODBAM,
            protocol_run_token="no-approved-protocol",
            workflow_release_id=_LOCAL_WORKFLOW_ID,
            artifacts=(
                ArtifactCommitment(
                    role="analysis_bam",
                    artifact_token="local-analysis-bam",
                    size_bytes=int(sealed[context.bam_name]["size_bytes"]),
                    provider_hmac_sha256=bam_hmac,
                ),
                ArtifactCommitment(
                    role=TOOL_ROLE,
                    artifact_token=f"{tool['identity']['tool_id']}-{tool['identity']['version']}",
                    size_bytes=int(tool["size_bytes"]),
                    provider_hmac_sha256=tool_hmac,
                ),
            ),
        )
        match = next(check.outcome for check in report.checks if check.code == "TBX-BAM-002")
        bundle = build_result_bundle(
            stage.attempt_dir / "bundle",
            measurement=measurement,
            provenance=provenance,
            method=BundleMethodIdentity(
                method_id=definition.method_id,
                version=definition.version,
                method_definition_sha256=method_definition_sha256(definition),
            ),
            signing_key=context.signing_key,
            reference_match=(
                "registered_digests" if match == PreflightOutcome.PASS else "name_and_length_only"
            ),
        )
        files = sorted(path for path in bundle.rglob("*") if path.is_file())
        return StageResult(
            outputs={
                f"bundle_{index:02d}": path.relative_to(stage.attempt_dir).as_posix()
                for index, path in enumerate(files)
            },
            metadata={
                "signed": True,
                "development_trust_only": True,
                "data_origin": "local_unqualified",
            },
        )


def _resolve_modkit() -> Any:
    from .toolchain import resolve_modkit

    return resolve_modkit()


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


def _floor_refusal(cause: str) -> Exception:
    return _refusal(
        "TBX-METH-004",
        "Too few marker fragments to estimate a mixture",
        cause=cause,
        fix=(
            "Sequence deeper or pool more input; the floors are locked method "
            "parameters, confirmed by the scientist"
        ),
    )


def _not_converged(cause: str) -> Exception:
    return _refusal(
        "TBX-METH-006",
        "The mixture fit did not converge",
        cause=cause,
        fix="Retrying will not change it; report the code with the support bundle",
    )


def compute_measurement(
    *,
    parameters: CellOriginParametersV1,
    reference_id: str,
    modbase_model: ModbaseModel,
    bam: Path,
    fasta: Path,
    atlas: Path,
    markers: Path,
    regions: Path,
    job_directory: Path,
    modkit: Any,
    heartbeat: Callable[[], None] | None = None,
) -> CellOriginMeasurementV1:
    """Pre-filter, extract, classify, fit and bootstrap one sealed BAM.

    Every refusal is a ``LocalStageRefusal`` with its method code
    (TBX-METH-001 to 007); a modkit timeout is retryable.  modkit's work files live under ``job_directory``
    and are removed before this returns.
    """

    from evidence_inspector.cell_origin_inputs import (
        CellOriginInputError,
        load_modkit_extract_calls,
    )
    from evidence_inspector.cell_origin_models import DigestArtifact, NnlsRowScale
    from evidence_inspector.cell_origin_pipeline import (
        NORMALIZED_MODKIT_COLUMNS,
        CallCapExceeded,
        CellOriginPipelineError,
        ModkitKilled,
        ModkitTimedOut,
        NoMarkerAlignments,
        PipelineConfig,
        _extract_modbam,
        _filtered_atlas,
        _load_loyfer_resources,
        _modkit_work_directory,
        _partitioned_classification,
        _remove_work_directory,
        validation_report,
    )
    from evidence_inspector.deconvolution import (
        DeconvolutionError,
        bootstrap_uxm_v2,
        deconvolve_uxm_v2,
    )

    caps = parameters.caps
    config = PipelineConfig(
        marker_bed=regions,
        marker_metadata=markers,
        atlas_u_matrix=atlas,
        output_path=job_directory / "unused-standalone-result.json",
        aligned_modbam=bam,
        maximum_calls=caps.maximum_calls,
        maximum_groups=caps.maximum_groups,
        maximum_cpgs_per_group=caps.maximum_cpgs_per_group,
        bootstrap_replicates=parameters.bootstrap_replicates,
        random_seed=parameters.bootstrap_random_seed,
        reference_fasta=fasta,
        job_directory=job_directory,
        modkit_filter_threshold=parameters.modkit_filter_threshold,
        min_mapq=parameters.min_mapq,
    )
    beat = heartbeat or (lambda: None)
    try:
        resources = _load_loyfer_resources(config)
    except CellOriginPipelineError as exc:
        raise _refusal(
            "TBX-ASSET-005",
            "The job's copies of the Loyfer assets do not agree with each other",
            cause=str(exc),
            fix="Register the three unmodified Loyfer files (method-asset register --from-dir)",
        ) from exc
    work = _modkit_work_directory(config)
    try:
        try:
            extract, prefilter = _extract_modbam(config, modkit=modkit)
        except CallCapExceeded as exc:
            raise _refusal(
                "TBX-METH-005",
                "The input exceeds a locked cell-origin cap",
                cause=f"modkit emitted more than {caps.maximum_calls} CpG calls",
                fix="The caps are locked method parameters; a larger input needs a new "
                "method version",
            ) from exc
        except NoMarkerAlignments as exc:
            raise _floor_refusal(
                "no alignment passed the pre-filter and overlapped a marker region"
            ) from exc
        except ModkitKilled as exc:
            raise _retryable(
                "TBX-JOB-001",
                "modkit was stopped before it finished",
                cause=str(exc),
                fix="Free memory on the host (or stop other work), then resume the job",
            ) from exc
        except ModkitTimedOut as exc:
            raise _retryable(
                "TBX-JOB-001",
                "modkit did not finish within its timeout",
                cause="the modkit extract timed out and was stopped",
                fix="Check the host is not overloaded or asleep, then resume the job",
            ) from exc
        except CellOriginPipelineError as exc:
            if isinstance(exc.__cause__, OSError):
                raise _retryable(
                    "TBX-JOB-001",
                    "Writing the pre-filtered BAM or the extract failed",
                    cause=f"{type(exc.__cause__).__name__} under the job directory",
                    fix="Free space on ROOT's volume (or fix its permissions), then resume "
                    "the job",
                ) from exc
            raise _refusal(
                "TBX-METH-001",
                "modkit could not extract modification calls from this BAM",
                cause=str(exc),
                fix="Run modkit validate on the aligned BAM; re-basecall or realign if "
                "its MM/ML tags are damaged",
            ) from exc
        beat()
        with extract.open("rb") as handle:
            extracted = max(sum(1 for _ in handle) - 1, 0)  # rows after the header
        try:
            calls = load_modkit_extract_calls(
                extract,
                columns=NORMALIZED_MODKIT_COLUMNS,
                # Read IDs are hashed in memory only; no output depends on the salt.
                fragment_hash_salt=os.urandom(32),
                max_rows=caps.maximum_calls,
            )
        except CellOriginInputError as exc:
            raise _refusal(
                "TBX-METH-001",
                "modkit's extract could not be read as CpG calls",
                cause=str(exc),
                fix="Run modkit validate on the aligned BAM",
            ) from exc
        if not calls:
            raise _floor_refusal("modkit extracted no eligible cytosine calls at the markers")
        classified = _partitioned_classification(
            calls,
            resources.markers,
            maximum_groups=caps.maximum_groups,
            maximum_cpgs_per_group=caps.maximum_cpgs_per_group,
        )
        beat()
    finally:
        _remove_work_directory(work)
    diagnostics = classified.diagnostics
    if diagnostics.partial_input or diagnostics.excluded_oversized_groups:
        raise _refusal(
            "TBX-METH-005",
            "The input exceeds a locked cell-origin cap",
            cause=(
                f"more than {caps.maximum_groups} fragment-marker groups, or a group "
                f"with more than {caps.maximum_cpgs_per_group} CpGs"
            ),
            fix="The caps are locked method parameters; a larger input needs a new "
            "method version",
        )
    rows = classified.marker_counts
    classified_fragments = sum(row.u_count + row.m_count for row in rows)
    if (
        not rows
        or classified_fragments < parameters.min_classified_fragments
        or len(rows) < parameters.min_observed_markers
    ):
        raise _floor_refusal(
            f"{classified_fragments} classified fragments at {len(rows)} of "
            f"{len(resources.markers)} markers; the floors are "
            f"{parameters.min_classified_fragments} fragments and "
            f"{parameters.min_observed_markers} markers"
        )
    atlas_rows = _filtered_atlas(resources.atlas, tuple(row.marker_id for row in rows))
    row_scale = NnlsRowScale(parameters.nnls_row_scale)
    try:
        fit = deconvolve_uxm_v2(
            rows,
            atlas_rows,
            row_scale=row_scale,
            tolerance=parameters.nnls_tolerance,
            max_iterations=parameters.nnls_max_iterations,
        )
    except DeconvolutionError as exc:
        if "did not converge" in str(exc):
            raise _not_converged(str(exc)) from exc
        raise _floor_refusal(f"the fit had no usable signal ({exc})") from exc
    if not fit.diagnostics.converged:
        raise _not_converged(
            f"the NNLS solver stopped after {fit.diagnostics.iterations} iterations "
            "without converging"
        )
    beat()
    bootstrap = bootstrap_uxm_v2(
        rows,
        atlas_rows,
        fit,
        replicates=parameters.bootstrap_replicates,
        random_seed=parameters.bootstrap_random_seed,
        confidence_level=parameters.bootstrap_confidence_level,
    )
    beat()
    copies = tuple(
        (
            DigestArtifact(
                artifact_id=f"artifact.{kind}",
                sha256=_sha256_file(path),
                size_bytes=path.stat().st_size,
            ),
            path,
        )
        for kind, path in (("loyfer-atlas", atlas), ("loyfer-markers", markers), ("loyfer-regions", regions))
    )
    report = validation_report(
        marker_counts=rows,
        classified_fragment_marker_count=diagnostics.classified_fragment_marker_count,
        atlas=atlas_rows,
        deconvolution=fit,
        bootstrap=bootstrap,
        artifacts=copies,
    )
    if not report.passed:
        failed = [record.check.value for record in report.records if not record.passed]
        raise _refusal(
            "TBX-METH-007",
            "The result failed its validation checks",
            cause="failed checks: " + ", ".join(failed),
            fix="Retrying will not change it; write `traceback support-bundle JOB_ID "
            "--output DIR` and report the code",
        )
    intervals = {item.cell_type_id: item for item in bootstrap.intervals}
    exclusions = {reason.value: count for reason, count in prefilter.excluded.items()}
    measurement = CellOriginMeasurementV1(
        definition_id=f"{DEFINITION_ID_PREFIX}.{reference_id}",
        approval_state=ApprovalState.UNAPPROVED_LOCAL,
        reference_id=reference_id,
        modbase_model=modbase_model,
        denominators=CellOriginDenominators(
            records_scanned=prefilter.records_scanned,
            alignment_exclusions=AlignmentExclusions(**exclusions),
            eligible_alignments=prefilter.passing,
            alignments_with_mod_tags=prefilter.passing_with_mod_tags,
            marker_overlapping_fragments=diagnostics.fragment_marker_group_count,
            classified_fragments=classified_fragments,
            mixed_fragments=sum(row.x_count for row in rows),
            excluded_fewer_than_4_cpgs=diagnostics.excluded_fewer_than_four_cpgs,
            registered_markers=len(resources.markers),
            atlas_markers_excluded_incomplete=resources.excluded_incomplete_count,
            observed_markers=len(rows),
        ),
        cpg_calls=CpgCalls(
            extracted=extracted,
            dropped_at_load=extracted - diagnostics.inspected_calls,
            inspected=diagnostics.inspected_calls,
            retained=diagnostics.retained_unique_cpg_calls,
            excluded_by_reason=CpgCallExclusions(
                duplicate_calls=diagnostics.duplicate_cpg_calls,
                failed_calls=diagnostics.excluded_failed_calls,
                non_c_calls=diagnostics.excluded_non_c_calls,
                unsupported_modification_calls=diagnostics.excluded_unsupported_modification_calls,
                invalid_calls=diagnostics.excluded_invalid_calls,
                outside_marker_calls=diagnostics.excluded_outside_marker_calls,
                ambiguous_marker_calls=diagnostics.excluded_ambiguous_marker_calls,
                unknown_marker_calls=diagnostics.excluded_unknown_marker_calls,
                conflicting_duplicate_groups=diagnostics.excluded_conflicting_duplicate_groups,
                oversized_groups=diagnostics.excluded_oversized_groups,
            ),
        ),
        marker_counts=tuple(
            MarkerCount(marker_id=row.marker_id, u=row.u_count, m=row.m_count, x=row.x_count)
            for row in rows
        ),
        estimates=tuple(
            sorted(
                (
                    ContributorEstimate(
                        contributor_id=item.cell_type_id,
                        fraction=item.fraction,
                        raw_nnls_weight=item.raw_nnls_weight,
                        interval=(
                            FractionInterval(
                                low=intervals[item.cell_type_id].lower_fraction,
                                high=intervals[item.cell_type_id].upper_fraction,
                            )
                            if intervals[item.cell_type_id].lower_fraction is not None
                            else None
                        ),
                        interval_state=intervals[item.cell_type_id].information_status.value,
                    )
                    for item in fit.estimates
                ),
                key=lambda item: item.contributor_id,
            )
        ),
        solver=SolverDiagnostics(
            converged=True,
            iterations=fit.diagnostics.iterations,
            residual_l2=fit.diagnostics.residual_l2,
            objective_value=fit.diagnostics.objective_value,
            row_scale=fit.diagnostics.row_scale.value,
        ),
    )
    from .export import ExportBoundaryError, validate_measurement

    try:
        validate_measurement(measurement)
    except ExportBoundaryError as exc:
        # Deterministic: the same sealed input fails the same way on retry.
        raise _refusal(
            "TBX-METH-007",
            "The result failed its validation checks",
            cause=f"failed checks: export_boundary ({exc})",
            fix="Check that --modbase-model and the atlas labels are plain identifiers; "
            "retrying will not change it",
        ) from exc
    return measurement


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


DEFAULT_ANALYSIS = CellOriginAnalysis()
register_measurement_schema(MEASUREMENT_SCHEMA)
register_analysis_stages(DEFAULT_ANALYSIS.spec)


__all__ = [
    "CHART_SCHEMA_VERSION",
    "DEFAULT_ANALYSIS",
    "LIMITATION_STATEMENTS",
    "MEASUREMENT_SCHEMA",
    "PATH_STEM",
    "SCHEMA_VERSION",
    "CellOriginAnalysis",
    "CellOriginChartV1",
    "CellOriginDenominators",
    "CellOriginLimitationsV1",
    "CellOriginMeasurementV1",
    "ModbaseModel",
    "build_chart",
    "build_limitations",
    "compute_measurement",
    "contig_refusal",
    "modification_refusal",
    "render_report",
    "resolve_modbase_model",
]
