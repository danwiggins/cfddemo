"""Copy number as a signed local record (signal methods CN3).

``run BAM --reference REF --analysis copy-number`` runs one job of three
stages over the sealed BAM copy:

1. validate: the three registered ichorCNA assets are copied into the job and
   hashed there (SH2); then the shared BAM preflight, the contig check
   (TBX-CNA-003: chr1-chr22, UCSC style) and the index depth floor
   (TBX-CNA-001: mapped records on chr1-chr22 in the ``.bai``, an upper bound
   on the reads that can count);
2. measure: a pysam pre-filter writes the eligible primary alignments into a
   private counting BAM (no read names, sequences or tags); below the
   counted-read floor the record is refused (TBX-CNA-002).  ``readCounter``
   bins them, and ichorCNA runs through the isolated Rscript runner (CN1) in
   a private staging directory.  Its outputs are validated by the adapter's
   parsers and replay (``validate_local_ichor_outputs``); a non-zero exit, a
   timeout or a validation failure refuses the record (TBX-CNA-004).  An
   unidentifiable solution is a record with ``identifiable: false``, not a
   failure;
3. sign: a ``result-bundle.v4`` record whose method identity is the locked
   ``mth_copy_number_ichorcna`` definition.

A missing or changed toolchain is retryable (TBX-TOOL-002): installing it and
resuming finishes the job.  The ``.RData`` workspace and the PDFs ichorCNA
writes are never opened, hashed, signed or kept: they are not byte-stable.

The measurement is :class:`CopyNumberMeasurementV1`, registered for v4 at
``measurements/copy-number.v1.json`` and ``charts/copy-number.v1.json``.
Every record is unqualified, local, not for clinical use and descriptive only.

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
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from .analyses import COPY_NUMBER, AnalysisStages, ReadinessRow, register_analysis_stages
from .contracts import ApprovalState, RunnerContract
from .copy_number_method import (
    ARGV_COUNTING_BAM,
    AUTOSOMES,
    METHOD_SLUG,
    STATED_LOWER_LIMIT_BASIS,
    STATED_LOWER_LIMIT_VALUE,
    CopyNumberParametersV1,
    copy_number_asset_files,
    copy_number_method_definition,
    default_parameters,
    registered_copy_number_assets,
)
from .export import LOCAL_REPORT_BANNER, ReferenceMatch
from .measurement_schemas import (
    BundleMeasurementSchema,
    LocalCatalogBinding,
    RecordViewBinding,
    register_measurement_schema,
)
from .references import AssetKind
from .serialization import canonical_json_bytes

SCHEMA_VERSION = "traceback.copy-number-measurement.v1"
CHART_SCHEMA_VERSION = "traceback.copy-number-chart.v1"
LIMITATIONS_SCHEMA_VERSION = "traceback.copy-number-limitations.v1"
VIEW_SCHEMA_VERSION = "traceback.local-copy-number-view.v1"
LIMITATIONS_TEMPLATE = "local-copy-number-research-use.v1"
PATH_STEM = "copy-number.v1"
DEFINITION_ID_PREFIX = "copy-number-ichorcna-v1"
STAGE_VERSION = "1"
SAMPLE_ID = "sample"
PREFLIGHT_OUTPUT = "copy-number-preflight.json"
MEASUREMENT_OUTPUT = "measurement.json"
TOOL_OUTPUT = "tool.json"
COUNTS_OUTPUT = "read-counts.wig"
OUTPUTS_DIRECTORY = "ichor-outputs"
STAGING_DIRECTORY = "ichor-staging"
COUNTING_DIRECTORY = "counting"
TOOL_ROLE = "tool_binary"
# The ichorCNA text outputs a record keeps (by role); the .RData is not one.
KEPT_OUTPUT_ROLES = (
    "bin_level_cna",
    "combined_corrected_depth",
    "parameters_and_candidates",
    "segments_detailed",
    "segments_raw",
)
_ABORT_CHECK_SECONDS = 30.0

# The fixed limitation statements (spec §3.2).  "Diagnosis" and "healthy"
# stay out: every record text passes the export claim check.
LIMITATION_STATEMENTS: tuple[str, ...] = (
    "ichorCNA tumour-fraction estimate from a model built for shallow short-read "
    "sequencing.",
    "The published lower limit is about 3% at about 0.1x short-read coverage "
    "(Adalsteinsson et al., Nat Commun 2017). It is not established for this nanopore "
    "protocol.",
    "No reference panel (PoN): bin-level noise is not corrected against a panel of "
    "non-tumour samples.",
    "Autosomes only.",
    "Descriptive, unqualified, local, not for clinical use.",
)

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
Count = Annotated[int, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


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


class CopyNumberCounts(RunnerContract):
    """How much data the estimate rests on.

    ``records_scanned`` counts the alignment records placed on chr1-chr22;
    ``counted_reads`` are the eligible primary alignments readCounter binned.
    Bins: ``bins_total`` on the grid, ``bins_masked`` removed before the model
    (centromere or flank, low mappability), ``bins_used`` with a corrected
    log2 value and ``bins_without_value`` kept but without one (ichorCNA NA).
    """

    records_scanned: Count
    alignment_exclusions: AlignmentExclusions
    counted_reads: Count
    bins_total: Annotated[int, Field(ge=1)]
    bins_used: Count
    bins_masked: Count
    bins_without_value: Count

    @model_validator(mode="after")
    def reconcile(self) -> CopyNumberCounts:
        if self.records_scanned - self.alignment_exclusions.total != self.counted_reads:
            raise ValueError("counted reads must be the scanned minus the excluded")
        if self.bins_used + self.bins_masked + self.bins_without_value != self.bins_total:
            raise ValueError("bins must be used, masked or without a value")
        return self


class CopyNumberBin(RunnerContract):
    """One grid bin, zero-based half-open; masked bins carry no value."""

    chr: Identifier
    start: Count
    end: Annotated[int, Field(ge=1)]
    log2_corrected: Finite | None
    mask: Literal["centromere_or_flank", "low_mappability"] | None

    @model_validator(mode="after")
    def coherent(self) -> CopyNumberBin:
        if self.end <= self.start:
            raise ValueError("bin end must exceed start")
        if self.mask is not None and self.log2_corrected is not None:
            raise ValueError("a masked bin carries no value")
        return self


class CopyNumberSegment(RunnerContract):
    chr: Identifier
    start: Count
    end: Annotated[int, Field(ge=1)]
    n_bins: Annotated[int, Field(ge=1)]
    median_log2: Finite
    copy_number: Count
    call: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class CopyNumberSolution(RunnerContract):
    """ichorCNA's selected solution.

    ``model_fraction`` is ichorCNA's tumour-fraction estimate.  When the
    adapter's replay finds too little altered structure (``identifiable``
    false) ichorCNA forces it to 0 and the view shows no estimate.
    ``selection_resolved`` is false when several starting points round to the
    selected summary (the estimate is the same; which start won is not).
    """

    model_fraction: Fraction
    ploidy: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    pon_mode: Literal["none_development"]
    identifiable: bool
    selection_resolved: bool

    @model_validator(mode="after")
    def forced_zero(self) -> CopyNumberSolution:
        if not self.identifiable and self.model_fraction != 0:
            raise ValueError("an unidentifiable solution carries a model fraction of 0")
        return self


class StatedLowerLimit(RunnerContract):
    value: Literal[0.03] = STATED_LOWER_LIMIT_VALUE
    basis: Literal[
        "About 3% at about 0.1x short-read coverage with a reference panel (PoN) "
        "(Adalsteinsson et al., Nat Commun 2017); not established for this nanopore "
        "protocol."
    ] = STATED_LOWER_LIMIT_BASIS


class CopyNumberMeasurementV1(RunnerContract):
    """One BAM's ichorCNA copy-number profile and model solution."""

    schema_version: Literal["traceback.copy-number-measurement.v1"] = SCHEMA_VERSION
    definition_id: Identifier
    approval_state: Literal[ApprovalState.UNAPPROVED_LOCAL]
    reference_id: Identifier
    counts: CopyNumberCounts
    bins: tuple[CopyNumberBin, ...] = Field(min_length=1)
    segments: tuple[CopyNumberSegment, ...]
    solution: CopyNumberSolution
    stated_lower_limit: StatedLowerLimit

    @model_validator(mode="after")
    def coherent(self) -> CopyNumberMeasurementV1:
        if self.definition_id != f"{DEFINITION_ID_PREFIX}.{self.reference_id}":
            raise ValueError("definition_id must name the reference")
        order = {contig: index for index, contig in enumerate(AUTOSOMES)}
        keys = [(row.chr, row.start) for row in self.bins]
        if any(row.chr not in order for row in self.bins):
            raise ValueError("bins are on chr1-chr22 only")
        if keys != sorted(keys, key=lambda item: (order[item[0]], item[1])) or len(
            set(keys)
        ) != len(keys):
            raise ValueError("bins are unique and in genome order")
        counts = self.counts
        if counts.bins_total != len(self.bins):
            raise ValueError("bins_total must equal the bins")
        if counts.bins_masked != sum(row.mask is not None for row in self.bins):
            raise ValueError("bins_masked must equal the masked bins")
        if counts.bins_used != sum(row.log2_corrected is not None for row in self.bins):
            raise ValueError("bins_used must equal the bins with a value")
        contigs = {row.chr for row in self.bins}
        if any(segment.chr not in contigs for segment in self.segments):
            raise ValueError("segments lie on binned chromosomes")
        return self


class CopyNumberChartContig(RunnerContract):
    chr: Identifier
    genome_offset: Count
    span: Annotated[int, Field(ge=1)]


class CopyNumberChartPoint(RunnerContract):
    chr: Identifier
    start: Count
    log2: Finite


class CopyNumberChartV1(RunnerContract):
    """Genome-wide log2 points and segments, derived losslessly from the measurement."""

    schema_version: Literal["traceback.copy-number-chart.v1"] = CHART_SCHEMA_VERSION
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contigs: tuple[CopyNumberChartContig, ...] = Field(min_length=1)
    points: tuple[CopyNumberChartPoint, ...]
    segments: tuple[CopyNumberSegment, ...]
    counted_reads: Count
    bins_used: Count
    bins_total: Annotated[int, Field(ge=1)]


class CopyNumberLimitationsV1(RunnerContract):
    schema_version: Literal["traceback.copy-number-limitations.v1"] = LIMITATIONS_SCHEMA_VERSION
    template_id: Literal["local-copy-number-research-use.v1"] = LIMITATIONS_TEMPLATE
    reference_match: ReferenceMatch
    statements: tuple[str, ...] = LIMITATION_STATEMENTS

    @model_validator(mode="after")
    def fixed_statements(self) -> CopyNumberLimitationsV1:
        if self.statements != LIMITATION_STATEMENTS:
            raise ValueError("copy-number limitations are the fixed statements")
        return self


class CopyNumberViewBody(RunnerContract):
    """The minimal record view (CN5 builds the plot): basis and states only."""

    counted_reads: Count
    bins_used: Count
    bins_total: Annotated[int, Field(ge=1)]
    bins_masked: Count
    segments: Count
    pon_mode: Literal["none_development"]
    identifiable: bool
    stated_lower_limit: StatedLowerLimit


# ---------------------------------------------------------------------------
# Chart, limitations, report, catalog denominator, view (pure functions)
# ---------------------------------------------------------------------------


def build_chart(measurement: CopyNumberMeasurementV1, sha256: str) -> CopyNumberChartV1:
    spans: dict[str, int] = {}
    for row in measurement.bins:
        spans[row.chr] = max(spans.get(row.chr, 0), row.end)
    offset = 0
    contigs = []
    for contig, span in spans.items():
        contigs.append(CopyNumberChartContig(chr=contig, genome_offset=offset, span=span))
        offset += span
    return CopyNumberChartV1(
        measurement_sha256=sha256,
        contigs=tuple(contigs),
        points=tuple(
            CopyNumberChartPoint(chr=row.chr, start=row.start, log2=row.log2_corrected)
            for row in measurement.bins
            if row.log2_corrected is not None
        ),
        segments=measurement.segments,
        counted_reads=measurement.counts.counted_reads,
        bins_used=measurement.counts.bins_used,
        bins_total=measurement.counts.bins_total,
    )


def build_limitations(
    measurement: CopyNumberMeasurementV1, reference_match: ReferenceMatch
) -> CopyNumberLimitationsV1:
    return CopyNumberLimitationsV1(reference_match=reference_match)


def render_report(
    measurement: CopyNumberMeasurementV1, limitations: CopyNumberLimitationsV1
) -> bytes:
    """The minimal local report: identity, basis and limitations.

    No tumour-fraction value is printed here (gate G1); it is in the signed
    measurement file with its stated lower limit.
    """

    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    counts = measurement.counts
    reference_item = (
        "<li>The reference was matched by contig name and length only; the input "
        "header carried no sequence digests to compare.</li>"
        if limitations.reference_match == "name_and_length_only"
        else ""
    )
    statements = "".join(f"<li>{escape(item)}</li>" for item in limitations.statements)
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback local copy-number record</title>"
        f"<p role=\"note\"><strong>{escape(LOCAL_REPORT_BANNER)}</strong></p>"
        "<h1>Local copy-number research record</h1>"
        f"<p>Definition: <code>{escape(measurement.definition_id)}</code>; reference: "
        f"<code>{escape(measurement.reference_id)}</code>.</p>"
        f"<p>Based on {counts.counted_reads} counted reads of {counts.records_scanned} "
        f"records scanned on chr1-chr22, in {counts.bins_used} of {counts.bins_total} "
        f"bins ({counts.bins_masked} masked).</p>"
        "<p>Reference panel (PoN): none.</p>"
        "<p>The ichorCNA tumour-fraction estimate and its stated lower limit are in the "
        "signed measurement file of this record.</p>"
        "<h2>Limitations</h2><ul>"
        "<li>The measurement method is unqualified for this input.</li>"
        "<li>The record is signed with a development key only; it carries no "
        "production trust.</li>"
        f"{reference_item}{statements}</ul></html>"
    )
    return body.encode("utf-8")


def _denominator_ledger(verified: Any) -> Any:
    """The E06 ledger in alignment units: scanned, accepted, counted."""

    from evidence_inspector.result_view import (
        AttritionReason,
        AttritionStage,
        CountState,
        CountValue,
        DenominatorLedger,
    )

    counts = verified.measurement.counts
    excluded = counts.alignment_exclusions

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
    accepted = counts.records_scanned - sum(item[1] for item in acceptance)
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
        input_records=observed(counts.records_scanned, "Records scanned on chr1-chr22"),
        accepted_records=observed(accepted, "Primary mapped alignments"),
        eligible_records=observed(counts.counted_reads, "Counted reads"),
        displayed_records=observed(counts.counted_reads, "Displayed reads"),
        attrition=attrition,
    )


def _no_reference_store(root: Any, registered: Any) -> Any:
    raise ValueError("copy-number records bind their own hash-keyed method store")


def build_view_body(measurement: CopyNumberMeasurementV1) -> CopyNumberViewBody:
    counts = measurement.counts
    return CopyNumberViewBody(
        counted_reads=counts.counted_reads,
        bins_used=counts.bins_used,
        bins_total=counts.bins_total,
        bins_masked=counts.bins_masked,
        segments=len(measurement.segments),
        pon_mode=measurement.solution.pon_mode,
        identifiable=measurement.solution.identifiable,
        stated_lower_limit=measurement.stated_lower_limit,
    )


MEASUREMENT_SCHEMA = BundleMeasurementSchema(
    schema_version=SCHEMA_VERSION,
    path_stem=PATH_STEM,
    measurement_model=CopyNumberMeasurementV1,
    chart_model=CopyNumberChartV1,
    limitations_model=CopyNumberLimitationsV1,
    build_chart=build_chart,
    build_limitations=build_limitations,
    render_report=render_report,
    catalog=LocalCatalogBinding(
        result_schema_id="schema_copy_number_measurement",
        result_schema_version="1.0.0",
        accessible_label="Copy number, unqualified local record",
        normalization_semantics_id="sem_ichorcna_corrected_log2",
        coordinate_semantics_id="sem_ichorcna_hg38_bins",
        denominator_semantics_id="sem_counted_alignments",
        authority=_no_reference_store,
        denominator=_denominator_ledger,
        method_slug=METHOD_SLUG,
    ),
    record_view=RecordViewBinding(
        analysis="copy_number",
        view_schema_version=VIEW_SCHEMA_VERSION,
        key_count_unit="reads counted",
        key_count=lambda measurement: measurement.counts.counted_reads,
        build_body=build_view_body,
    ),
)


# ---------------------------------------------------------------------------
# Stage refusals
# ---------------------------------------------------------------------------


def _refusal(code: str, summary: str, *, cause: str, fix: str) -> Exception:
    from .cli import LocalStageRefusal

    return LocalStageRefusal(code, summary + "; no record was made", cause=cause, fix=fix)


def _retryable(problem: Any) -> Exception:
    """A stage problem a retry can fix (a tool, a moved file): the job can resume."""

    from .cli import ToolUnavailableAtStage

    return ToolUnavailableAtStage(problem)


def _ichor_failure(cause: str) -> Exception:
    return _refusal(
        "TBX-CNA-004",
        "ichorCNA did not produce a valid result",
        cause=cause,
        fix=(
            "Retrying will not change it; write `traceback support-bundle JOB_ID --output "
            "DIR` for this job and report the code; other analyses are unaffected"
        ),
    )


def contig_refusal(contigs: Sequence[str], counting_contigs: Sequence[str]) -> Exception | None:
    """TBX-CNA-003 unless every counting contig (chr1-chr22) is present."""

    missing = [name for name in counting_contigs if name not in set(contigs)]
    if not missing:
        return None
    shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    return _refusal(
        "TBX-CNA-003",
        "Copy number needs UCSC-style chr1-chr22 contigs",
        cause=f"contigs not in the BAM header: {shown}",
        fix=(
            "Align to an hg38 reference with UCSC contig names (chr1..chr22), register it, "
            "and run again; the fragment analysis is unaffected"
        ),
    )


def depth_refusal(mapped_records: int, floor: int) -> Exception | None:
    """TBX-CNA-001 when the index's mapped records on chr1-chr22 are below the floor."""

    if mapped_records >= floor:
        return None
    return _refusal(
        "TBX-CNA-001",
        "Too few mapped reads on chr1-chr22 for copy number",
        cause=(
            f"the BAM index reports {mapped_records} mapped records on chr1-chr22, below "
            f"the locked floor of {floor} counted reads"
        ),
        fix="Sequence deeper or pool runs of the same sample; the fragment analysis is unaffected",
    )


def counted_refusal(counted_reads: int, floor: int) -> Exception | None:
    """TBX-CNA-002 when the counted reads are below the floor."""

    if counted_reads >= floor:
        return None
    return _refusal(
        "TBX-CNA-002",
        "Too few counted reads for copy number",
        cause=(
            f"{counted_reads} eligible primary alignments at MAPQ >= 20 on chr1-chr22, below "
            f"the locked floor of {floor}"
        ),
        fix="Sequence deeper or pool runs of the same sample; the fragment analysis is unaffected",
    )


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def index_mapped_records(
    bam: Path, contigs: Sequence[str], *, index: Path | None = None
) -> int:
    """Mapped records on ``contigs`` according to the BAM index (an upper bound).

    The index counts secondary, supplementary, duplicate and low-MAPQ records
    too, so the counted reads can only be fewer.
    """

    import pysam

    wanted = set(contigs)
    with pysam.AlignmentFile(
        str(bam), "rb", check_sq=False, index_filename=None if index is None else str(index)
    ) as reader:
        return sum(
            item.mapped for item in reader.get_index_statistics() if item.contig in wanted
        )


def write_counting_bam(
    source: Path,
    destination: Path,
    contigs: Sequence[str],
    *,
    min_mapq: int,
    index: Path | None = None,
) -> tuple[int, AlignmentExclusions, int]:
    """Copy the eligible primary alignments on ``contigs`` into ``destination``.

    Only the position, MAPQ and CIGAR of each alignment are written: no read
    name, sequence, quality or tag.  Returns ``(records_scanned, exclusions,
    counted_reads)``; ``destination`` is sorted and indexed.
    """

    import pysam

    from evidence_inspector.cell_origin_prefilter import prefilter_exclusion

    excluded = {reason: 0 for reason in AlignmentExclusions.model_fields}
    scanned = written = 0
    with pysam.AlignmentFile(
        str(source), "rb", index_filename=None if index is None else str(index)
    ) as reader:
        header = pysam.AlignmentHeader.from_dict(
            {
                "HD": {"VN": "1.6", "SO": "coordinate"},
                "SQ": [
                    {"SN": name, "LN": reader.get_reference_length(name)}
                    for name in reader.references
                ],
            }
        )
        with pysam.AlignmentFile(str(destination), "wb", header=header) as writer:
            for contig in contigs:
                reference_id = header.get_tid(contig)
                for record in reader.fetch(contig):
                    scanned += 1
                    reason = prefilter_exclusion(
                        record.flag, record.mapping_quality, min_mapq=min_mapq
                    )
                    if reason is not None:
                        excluded[reason.value] += 1
                        continue
                    stripped = pysam.AlignedSegment(header)
                    stripped.query_name = "r"
                    stripped.flag = 0
                    stripped.reference_id = reference_id
                    stripped.reference_start = record.reference_start
                    stripped.mapping_quality = record.mapping_quality
                    stripped.cigartuples = record.cigartuples
                    writer.write(stripped)
                    written += 1
    pysam.index(str(destination))
    return scanned, AlignmentExclusions(**excluded), written


def _abort_after_lost_lease(stage: Any) -> Callable[[], bool]:
    """``should_abort`` for a contained tool: renew the lease every 30 s; abort once lost."""

    last = [time.monotonic()]

    def should_abort() -> bool:
        if time.monotonic() - last[0] < _ABORT_CHECK_SECONDS:
            return False
        last[0] = time.monotonic()
        try:
            stage.heartbeat()
        except Exception:  # noqa: BLE001 - any renewal failure means the lease is gone
            return True
        return False

    return should_abort


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def _resolve_toolchain() -> Any:
    from .toolchain import resolve_copy_number_toolchain

    return resolve_copy_number_toolchain()


def _verify_toolchain_deep() -> None:
    """Every installed file, the package set and the R data packages (slow)."""

    from .toolchain import pin_for, resolve_ichor, toolchain_cache_root

    resolve_ichor(pin_for("ichor"), cache_root=toolchain_cache_root(), check="deep")


def depth_readiness(bam: Path, index: Path | None = None, floor: int | None = None) -> ReadinessRow:
    """``preflight --analysis copy-number``: the TBX-CNA-001 index depth floor."""

    floor = default_parameters().min_counted_reads if floor is None else floor
    try:
        mapped = index_mapped_records(bam, AUTOSOMES, index=index)
    except (OSError, ValueError) as exc:
        return ReadinessRow(
            "TBX-CNA-001", "blocked", f"the BAM index could not be read ({type(exc).__name__})"
        )
    refusal = depth_refusal(mapped, floor)
    if refusal is None:
        return ReadinessRow("TBX-CNA-001", "ready", "index depth on chr1-chr22 meets the floor")
    return ReadinessRow("TBX-CNA-001", "blocked", refusal.cause)  # type: ignore[attr-defined]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class CopyNumberAnalysis:
    """The copy-number method's SH4 stages, parameters and tool resolver.

    ``parameters()`` returns the locked parameters (tests pass other floors
    and timeouts); ``toolchain()`` returns the verified ichorCNA toolchain;
    ``platform_name`` selects the pin (tests only).
    """

    def __init__(
        self,
        *,
        parameters: Callable[[], CopyNumberParametersV1] = default_parameters,
        toolchain: Callable[[], Any] | None = None,
        verify_toolchain_deep: Callable[[], None] = _verify_toolchain_deep,
        platform_name: str | None = None,
    ) -> None:
        self.parameters = parameters
        self.toolchain = toolchain
        self.verify_toolchain_deep = verify_toolchain_deep
        self.platform_name = platform_name
        self.spec = AnalysisStages(
            analysis=COPY_NUMBER,
            method_slug=METHOD_SLUG,
            definition=self.definition,
            stages=self.stages,
            readiness=self.readiness,
            takes_root=True,
        )

    # -- method ------------------------------------------------------------

    def pin(self) -> Any:
        from .toolchain import pin_for

        return pin_for("ichor", self.platform_name)

    def definition(self, loaded: Any, config: Mapping[str, str], *, root: Path) -> Any:
        parameters = self.parameters()
        pin = self.pin()
        return copy_number_method_definition(
            loaded.registered,
            registered_copy_number_assets(
                root, pin.lock_sha256, bin_size_bp=parameters.bin_size_bp
            ),
            pin,
            parameters,
        )

    def readiness(
        self, loaded: Any, config: Mapping[str, str], *, root: Path
    ) -> list[ReadinessRow]:
        """Toolchain (TBX-TOOL-002), assets (TBX-ASSET-004), contigs (TBX-CNA-003).

        The depth floor (TBX-CNA-001) needs the BAM's index and is checked when
        the job runs.
        """

        from .references import ReferenceProblem, load_asset
        from .toolchain import ToolProblem

        rows: list[ReadinessRow] = []
        try:
            (self.toolchain or _resolve_toolchain)()
        except ToolProblem as problem:
            rows.append(
                ReadinessRow(
                    "TBX-TOOL-002",
                    "not_set_up" if problem.reason == "missing" else "blocked",
                    f"copy-number toolchain {problem.reason.replace('_', ' ')}; next: "
                    "traceback toolchain install copy-number --yes",
                )
            )
        else:
            rows.append(ReadinessRow("TBX-TOOL-002", "ready", "copy-number toolchain verified"))
        try:
            pin = self.pin()
        except ToolProblem:
            return rows
        parameters = self.parameters()
        files = copy_number_asset_files(pin.lock_sha256, bin_size_bp=parameters.bin_size_bp)
        missing = []
        for kind, (_, asset_id) in files.items():
            try:
                load_asset(root, asset_id, kind=kind)
            except ReferenceProblem:
                missing.append(kind.value)
        rows.append(
            ReadinessRow("TBX-ASSET-004", "ready", "ichorCNA assets registered")
            if not missing
            else ReadinessRow(
                "TBX-ASSET-004",
                "not_set_up",
                f"{', '.join(missing)} not registered; next: traceback method-asset "
                "register --from-toolchain copy-number --root <same-root>",
            )
        )
        refusal = contig_refusal(
            [contig.name for contig in loaded.registered.contigs], parameters.counting_contigs
        )
        rows.append(
            ReadinessRow("TBX-CNA-003", "ready", "chr1-chr22 registered")
            if refusal is None
            else ReadinessRow("TBX-CNA-003", "blocked", refusal.cause)  # type: ignore[attr-defined]
        )
        return rows

    # -- stages -----------------------------------------------------------

    def stages(self, context: Any) -> tuple[Any, ...]:
        from evidence_inspector.method_registry import method_definition_sha256

        from .contracts import StageName
        from .runner import StageSpec

        parameters = self.parameters()
        definition = self.definition(context.loaded, context.config, root=context.root)
        common = {
            "definition_sha256": method_definition_sha256(definition),
            "data_origin": "local_unqualified",
        }
        return (
            StageSpec(
                name=StageName.VALIDATE,
                version=STAGE_VERSION,
                callback=lambda stage: self._validate(stage, context, definition, parameters),
                parameters=common,
            ),
            StageSpec(
                name=StageName.MEASURE,
                version=STAGE_VERSION,
                callback=lambda stage: self._measure(stage, context, definition, parameters),
                parameters=common,
            ),
            StageSpec(
                name=StageName.SIGN,
                version=STAGE_VERSION,
                callback=lambda stage: self._sign(stage, context, definition),
                parameters={**common, "key_id": context.signing_key.key_id},
            ),
        )

    def _validate(
        self, stage: Any, context: Any, definition: Any, parameters: CopyNumberParametersV1
    ) -> Any:
        import pysam

        from .cli import _LOCAL_PREFLIGHT_POLICY, StaleLease, TerminalStageError
        from .contracts import PreflightOutcome
        from .preflight import BamPreflightPolicy, validate_bam_snapshot
        from .references import ReferenceProblem, copy_registered_asset, load_asset
        from .runner import StageResult

        # The registered assets, copied into this job and hashed here; each copy
        # must be the bytes the job's method definition names.
        bound = {item.asset_id: item.content_sha256 for item in definition.assets}
        lock = next(tool for tool in definition.tools if tool.tool_id == "tool_ichorcna_lock")
        copies: dict[str, str] = {}
        for kind, (_, asset_id) in copy_number_asset_files(
            lock.artifact_sha256, bin_size_bp=parameters.bin_size_bp
        ).items():
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
                copy = copy_registered_asset(context.root, asset_id, stage.attempt_dir, kind=kind)
            except ReferenceProblem as problem:
                raise _retryable(problem) from problem
            copies[kind.value] = copy.name
        registered = context.loaded.registered
        context.progress("STAGE  copy-number preflight: inspecting the sealed BAM copy")
        bam = stage.sealed_input_dir / context.bam_name
        try:
            report = validate_bam_snapshot(
                bam,
                stage.sealed_input_dir / context.index_name,
                registered,
                BamPreflightPolicy(policy_id=_LOCAL_PREFLIGHT_POLICY),
                compare_assembly=context.loaded.source.assembly_declared,
            )
            with pysam.AlignmentFile(str(bam), "rb", check_sq=False) as reader:
                contigs = tuple(reader.references)
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
        refusal = contig_refusal(contigs, parameters.counting_contigs)
        if refusal is not None:
            raise refusal
        mapped = index_mapped_records(
            bam, parameters.counting_contigs, index=stage.sealed_input_dir / context.index_name
        )
        refusal = depth_refusal(mapped, parameters.min_counted_reads)
        if refusal is not None:
            raise refusal
        output = stage.attempt_dir / PREFLIGHT_OUTPUT
        output.write_bytes(
            canonical_json_bytes(
                {
                    "preflight": report.model_dump(mode="json"),
                    "index_mapped_records": mapped,
                    "asset_copies": copies,
                }
            )
        )
        context.progress(f"STAGE  copy-number preflight {report.outcome.value}")
        return StageResult(
            outputs={
                "copy_number_preflight": output.name,
                **{f"asset_{kind.replace('-', '_')}": name for kind, name in copies.items()},
            },
            metadata={
                "preflight_outcome": report.outcome.value,
                "data_origin": "local_unqualified",
            },
        )

    def _measure(
        self, stage: Any, context: Any, definition: Any, parameters: CopyNumberParametersV1
    ) -> Any:
        from evidence_inspector.method_registry import method_definition_sha256

        from .runner import StageResult

        prior = json.loads((stage.prior_stage_dirs[0] / PREFLIGHT_OUTPUT).read_bytes())
        copies = {
            kind: stage.prior_stage_dirs[0] / name for kind, name in prior["asset_copies"].items()
        }
        toolchain = (self.toolchain or _resolve_toolchain)()
        tools = {tool.tool_id: tool.artifact_sha256 for tool in definition.tools}
        if (
            toolchain.identity.lock_sha256 != tools["tool_ichorcna_lock"]
            or toolchain.identity.driver_sha256 != tools["tool_ichorcna_driver"]
        ):
            raise _refusal(
                "TBX-JOB-003",
                "The installed toolchain differs from the job's method",
                cause="the verified toolchain is not the one this job's method definition names",
                fix="Run the input again with traceback run; the current method is a new job",
            )
        counting = stage.attempt_dir / COUNTING_DIRECTORY
        counting.mkdir(mode=0o700)
        context.progress("STAGE  copy-number measure: counting eligible primary alignments")
        scanned, exclusions, counted = write_counting_bam(
            stage.sealed_input_dir / context.bam_name,
            counting / "counting.bam",
            parameters.counting_contigs,
            min_mapq=parameters.min_mapq,
            index=stage.sealed_input_dir / context.index_name,
        )
        refusal = counted_refusal(counted, parameters.min_counted_reads)
        if refusal is not None:
            shutil.rmtree(counting)
            raise refusal
        counts_wig = stage.attempt_dir / COUNTS_OUTPUT
        self._read_counter(stage, toolchain, parameters, counting / "counting.bam", counts_wig)
        shutil.rmtree(counting)
        grid, run_sha256 = self._grid(
            method_definition_sha256(definition), parameters, copies, counts_wig, counted
        )
        context.progress("STAGE  copy-number measure: ichorCNA (isolated R)")
        result = self._ichor(stage, toolchain, parameters, copies, counts_wig, grid, run_sha256)
        measurement = build_measurement(
            result,
            reference_id=context.loaded.registered.reference_id,
            records_scanned=scanned,
            exclusions=exclusions,
            counted_reads=counted,
        )
        output = stage.attempt_dir / MEASUREMENT_OUTPUT
        output.write_bytes(canonical_json_bytes(measurement))
        tool_output = stage.attempt_dir / TOOL_OUTPUT
        tool_output.write_bytes(
            canonical_json_bytes(
                {
                    "identity": toolchain.identity.model_dump(mode="json"),
                    "installed": toolchain.installed.model_dump(mode="json"),
                    "readcounter_size_bytes": toolchain.readcounter.stat().st_size,
                    "rscript_size_bytes": toolchain.rscript.stat().st_size,
                }
            )
        )
        context.progress(
            f"STAGE  copy-number measure: {counted} counted reads in "
            f"{measurement.counts.bins_used} of {measurement.counts.bins_total} bins"
        )
        return StageResult(
            outputs={
                "copy_number_measurement": output.name,
                "tool": tool_output.name,
                "read_counts": counts_wig.name,
                **{
                    f"ichor_{item.role}": f"{OUTPUTS_DIRECTORY}/{item.relative_path}"
                    for item in result.artifacts
                },
            },
            metadata={
                "counted_reads": counted,
                "bins_used": measurement.counts.bins_used,
                "identifiable": measurement.solution.identifiable,
                "data_origin": "local_unqualified",
            },
        )

    def _read_counter(
        self,
        stage: Any,
        toolchain: Any,
        parameters: CopyNumberParametersV1,
        bam: Path,
        output: Path,
    ) -> None:
        from .contained_process import ContainedProcessError, run_contained
        from .references import ReferenceProblem

        argv = [
            str(toolchain.readcounter),
            *(
                str(bam) if item == ARGV_COUNTING_BAM else item
                for item in parameters.readcounter_arguments
            ),
        ]
        try:
            result = run_contained(
                argv,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                cwd=bam.parent,
                timeout_seconds=parameters.readcounter_timeout_seconds,
                should_abort=_abort_after_lost_lease(stage),
                stdout_path=output,
                stdout_limit_bytes=64 * 1024 * 1024,
            )
        except ContainedProcessError as exc:
            # A launch failure (a missing binary, EAGAIN) says nothing about
            # the input: retryable, as for Rscript's (TBX-TOOL-002).
            raise _retryable(
                ReferenceProblem(
                    "TBX-TOOL-002",
                    "readCounter could not start",
                    cause=str(exc),
                    fix="Run `traceback toolchain install copy-number --yes`, then resume the job",
                )
            ) from exc
        if result.outcome == "aborted":
            from .cli import StaleLease

            raise StaleLease("the worker lease was lost while readCounter ran")
        if result.outcome == "exited" and result.returncode != 0:
            # A damaged shared library leaves readCounter's own digest intact:
            # a toolchain problem (TBX-TOOL-002, retryable), not the input's.
            self.verify_toolchain_deep()
        if not result.succeeded or result.stdout_truncated:
            raise _ichor_failure(
                "readCounter "
                + (
                    "timed out"
                    if result.outcome == "timeout"
                    else "wrote too much output"
                    if result.stdout_truncated
                    else f"exited with status {result.returncode}"
                )
            )

    def _grid(
        self,
        definition_sha256: str,
        parameters: CopyNumberParametersV1,
        copies: Mapping[str, Path],
        counts_wig: Path,
        counted_reads: int,
    ) -> tuple[Any, str]:
        """The masked canonical grid of the counted contigs, and the run digest."""

        from evidence_inspector.ichor_adapter import (
            IchorOutputError,
            masked_canonical_grid,
            parse_centromere_table,
            parse_fixed_step_wig,
        )
        try:
            gc = parse_fixed_step_wig(copies[AssetKind.ICHOR_GC_WIG.value], "gc_wig")
            mappability = parse_fixed_step_wig(copies[AssetKind.ICHOR_MAP_WIG.value], "map_wig")
            intervals = parse_centromere_table(copies[AssetKind.ICHOR_CENTROMERE.value])
            counts = parse_fixed_step_wig(counts_wig, "raw_counts_wig")
        except IchorOutputError as exc:
            raise _ichor_failure(f"an ichorCNA input does not parse: {exc}") from exc
        wanted = set(parameters.counting_contigs)
        keep = [index for index, row in enumerate(gc.grid.bins) if row.contig in wanted]
        bins = [gc.grid.bins[index] for index in keep]
        if (
            gc.bin_size_bp != parameters.bin_size_bp
            or tuple(mappability.grid.bins) != tuple(gc.grid.bins)
            or tuple(counts.grid.bins) != tuple(bins)
        ):
            raise _ichor_failure(
                "readCounter's bins, the GC wig's and the mappability wig's are not one grid"
            )
        total = sum(counts.values)
        if total != counted_reads:
            raise _ichor_failure(
                f"readCounter binned {int(total)} reads; the pre-filter counted {counted_reads}"
            )
        order = tuple(contig for contig in parameters.counting_contigs if any(
            row.contig == contig for row in bins
        ))
        try:
            grid = masked_canonical_grid(
                bins,
                contig_order=order,
                intervals=intervals,
                map_values=[mappability.values[index] for index in keep],
                parameters=parameters.ichor,
                centromere_sha256=_sha256_file(copies[AssetKind.ICHOR_CENTROMERE.value]),
                map_sha256=_sha256_file(copies[AssetKind.ICHOR_MAP_WIG.value]),
            )
        except ValueError as exc:
            raise _ichor_failure(f"the analysis grid is invalid: {exc}") from exc
        run_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": "traceback.copy-number-local-run.v1",
                    "method_definition_sha256": definition_sha256,
                    "read_counts_sha256": _sha256_file(counts_wig),
                    "grid_sha256": grid.bin_definition_sha256,
                }
            )
        ).hexdigest()
        return grid, run_sha256

    def _ichor(
        self,
        stage: Any,
        toolchain: Any,
        parameters: CopyNumberParametersV1,
        copies: Mapping[str, Path],
        counts_wig: Path,
        grid: Any,
        run_sha256: str,
    ) -> Any:
        from evidence_inspector.ichor_adapter import (
            IchorOutputError,
            IchorPaths,
            ichor_driver_arguments,
            validate_local_ichor_outputs,
        )

        from .r_isolation import RInvocation, RIsolationError, run_isolated_r
        from .references import ReferenceProblem

        # The private staging directory replaces /input, /assets and /attempt:
        # plain copies, no symlinks, under this attempt only.
        staging = stage.attempt_dir / STAGING_DIRECTORY
        staging.mkdir(mode=0o700)
        staged = {
            "counts": staging / "counts.wig",
            "gc": staging / "gc.wig",
            "map": staging / "map.wig",
            "centromere": staging / "centromere.tsv",
        }
        shutil.copyfile(counts_wig, staged["counts"])
        shutil.copyfile(copies[AssetKind.ICHOR_GC_WIG.value], staged["gc"])
        shutil.copyfile(copies[AssetKind.ICHOR_MAP_WIG.value], staged["map"])
        shutil.copyfile(copies[AssetKind.ICHOR_CENTROMERE.value], staged["centromere"])
        out_dir = staging / "out"
        arguments = ichor_driver_arguments(
            parameters.ichor,
            sample_id=SAMPLE_ID,
            genome_build=parameters.genome_build,
            paths=IchorPaths(
                counts_wig=str(staged["counts"]),
                gc_wig=str(staged["gc"]),
                map_wig=str(staged["map"]),
                centromere=str(staged["centromere"]),
                panel_of_normals=None,
                out_dir=str(out_dir),
            ),
        )
        try:
            run = run_isolated_r(
                RInvocation(
                    rscript=toolchain.rscript,
                    script=toolchain.driver,
                    args=arguments,
                    library_paths=(toolchain.r_library,),
                    work_dir=staging,
                    timeout_seconds=parameters.ichor_timeout_seconds,
                    seed=parameters.r_seed,
                    rscript_sha256=toolchain.installed.rscript_sha256,
                    script_sha256=toolchain.installed.driver_sha256,
                ),
                should_abort=_abort_after_lost_lease(stage),
            )
        except RIsolationError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise _retryable(
                ReferenceProblem(
                    "TBX-TOOL-002",
                    "The ichorCNA toolchain changed since it was verified",
                    cause=str(exc),
                    fix="Run `traceback toolchain install copy-number --yes`, then resume the job",
                )
            ) from exc
        if run.process.outcome == "aborted":
            shutil.rmtree(staging, ignore_errors=True)
            from .cli import StaleLease

            raise StaleLease("the worker lease was lost while ichorCNA ran")
        if run.libpaths_mismatch:
            shutil.rmtree(staging, ignore_errors=True)
            raise _retryable(
                ReferenceProblem(
                    "TBX-TOOL-002",
                    "The ichorCNA toolchain's R library paths are not the declared ones",
                    cause="R started with library paths other than the toolchain's own",
                    fix="Run `traceback toolchain install copy-number --yes`, then resume the job",
                )
            )
        if not run.succeeded:
            shutil.rmtree(staging, ignore_errors=True)
            if run.process.outcome == "exited":
                # The shallow check covers readCounter, Rscript and the driver
                # only; a damaged R package also stops ichorCNA.  That is a
                # toolchain problem (TBX-TOOL-002, retryable), not the input's.
                self.verify_toolchain_deep()
            raise _ichor_failure(
                "ichorCNA timed out"
                if run.process.outcome == "timeout"
                else f"ichorCNA exited with status {run.process.returncode}"
            )
        try:
            result = validate_local_ichor_outputs(
                out_dir,
                sample_id=SAMPLE_ID,
                grid=grid,
                parameters=parameters.ichor,
                pon_mode=parameters.pon_mode,
                run_sha256=run_sha256,
            )
        except IchorOutputError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise _ichor_failure(f"the output failed validation: {exc}") from exc
        # Keep only the validated text outputs; the .RData, the PDFs and the
        # staging copies go unread.
        kept = stage.attempt_dir / OUTPUTS_DIRECTORY
        kept.mkdir(mode=0o700)
        for item in result.artifacts:
            target = kept / item.relative_path
            shutil.copyfile(out_dir / item.relative_path, target)
            if _sha256_file(target) != item.content_sha256:
                shutil.rmtree(staging, ignore_errors=True)
                raise _ichor_failure("an ichorCNA output changed while it was kept")
        shutil.rmtree(staging)
        return result

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

        context.progress("STAGE  copy-number sign: development-local key (development trust only)")
        prior = json.loads((stage.prior_stage_dirs[0] / PREFLIGHT_OUTPUT).read_bytes())
        report = PreflightReport.model_validate(prior["preflight"])
        measured = stage.prior_stage_dirs[-1]
        measurement = CopyNumberMeasurementV1.model_validate_json(
            (measured / MEASUREMENT_OUTPUT).read_bytes()
        )
        tool = json.loads((measured / TOOL_OUTPUT).read_bytes())
        manifest = json.loads((stage.sealed_input_dir / "input-manifest.local.json").read_bytes())
        sealed = {item["relative_path"]: item for item in manifest["files"]}
        key = _provenance_hmac_key(context.root)

        def commit(domain: bytes, sha256_hex: str) -> str:
            return hmac.new(key, domain + bytes.fromhex(sha256_hex), hashlib.sha256).hexdigest()

        version = tool["identity"]["version"]
        artifacts = sorted(
            (
                ArtifactCommitment(
                    role="analysis_bam",
                    artifact_token="local-analysis-bam",
                    size_bytes=int(sealed[context.bam_name]["size_bytes"]),
                    provider_hmac_sha256=commit(
                        b"traceback.provider-artifact.v1|",
                        sealed[context.bam_name]["sha256_local"],
                    ),
                ),
                ArtifactCommitment(
                    role="read_counts",
                    artifact_token="local-read-counts-wig",
                    size_bytes=(measured / COUNTS_OUTPUT).stat().st_size,
                    provider_hmac_sha256=commit(
                        b"traceback.provider-artifact.v1|",
                        _sha256_file(measured / COUNTS_OUTPUT),
                    ),
                ),
                # The installed binaries' digests, committed like the BAM (per-ROOT key).
                ArtifactCommitment(
                    role=TOOL_ROLE,
                    artifact_token=f"ichor-{version}-readcounter",
                    size_bytes=int(tool["readcounter_size_bytes"]),
                    provider_hmac_sha256=commit(
                        b"traceback.tool-binary.v1|", tool["installed"]["readcounter_sha256"]
                    ),
                ),
                ArtifactCommitment(
                    role=TOOL_ROLE,
                    artifact_token=f"ichor-{version}-rscript",
                    size_bytes=int(tool["rscript_size_bytes"]),
                    provider_hmac_sha256=commit(
                        b"traceback.tool-binary.v1|", tool["installed"]["rscript_sha256"]
                    ),
                ),
            ),
            key=lambda item: (item.role, item.artifact_token),
        )
        provenance = ExportRunProvenance(
            run_token=f"local-run-{stage.job_id[:16]}",
            input_kind=InputKind.MODBAM,
            protocol_run_token="no-approved-protocol",
            workflow_release_id=_LOCAL_WORKFLOW_ID,
            artifacts=tuple(artifacts),
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


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


def build_measurement(
    result: Any,
    *,
    reference_id: str,
    records_scanned: int,
    exclusions: AlignmentExclusions,
    counted_reads: int,
) -> CopyNumberMeasurementV1:
    """``CopyNumberMeasurementV1`` from the adapter's validated result."""

    masks = {
        (item.bin.contig, item.bin.start, item.bin.end): item.reason
        for item in result.canonical_grid.masks
    }
    bins = tuple(
        CopyNumberBin(
            chr=status.contig,
            start=status.start,
            end=status.end,
            log2_corrected=status.corrected_log2,
            mask=masks.get((status.contig, status.start, status.end)),
        )
        for status in result.bin_statuses
    )
    masked = sum(row.mask is not None for row in bins)
    used = sum(row.log2_corrected is not None for row in bins)
    selected = result.selected_solution
    return CopyNumberMeasurementV1(
        definition_id=f"{DEFINITION_ID_PREFIX}.{reference_id}",
        approval_state=ApprovalState.UNAPPROVED_LOCAL,
        reference_id=reference_id,
        counts=CopyNumberCounts(
            records_scanned=records_scanned,
            alignment_exclusions=exclusions,
            counted_reads=counted_reads,
            bins_total=len(bins),
            bins_used=used,
            bins_masked=masked,
            bins_without_value=len(bins) - used - masked,
        ),
        bins=bins,
        segments=tuple(
            CopyNumberSegment(
                chr=segment.contig,
                start=segment.start,
                end=segment.end,
                n_bins=segment.native_span_bin_count,
                median_log2=segment.median_log2,
                copy_number=segment.copy_number,
                call=segment.call,
            )
            for segment in result.segments
        ),
        solution=CopyNumberSolution(
            model_fraction=selected.model_fraction,
            ploidy=selected.ploidy,
            pon_mode=result.pon_mode,
            identifiable=result.status == "complete",
            selection_resolved=selected.selection_resolution == "resolved_unique_rounded_match",
        ),
        stated_lower_limit=StatedLowerLimit(),
    )


DEFAULT_ANALYSIS = CopyNumberAnalysis()
register_measurement_schema(MEASUREMENT_SCHEMA)
register_analysis_stages(DEFAULT_ANALYSIS.spec)


__all__ = [
    "CHART_SCHEMA_VERSION",
    "DEFAULT_ANALYSIS",
    "LIMITATION_STATEMENTS",
    "MEASUREMENT_SCHEMA",
    "SCHEMA_VERSION",
    "AlignmentExclusions",
    "CopyNumberAnalysis",
    "CopyNumberChartV1",
    "CopyNumberCounts",
    "CopyNumberLimitationsV1",
    "CopyNumberMeasurementV1",
    "build_chart",
    "build_measurement",
    "contig_refusal",
    "counted_refusal",
    "depth_readiness",
    "depth_refusal",
    "index_mapped_records",
    "render_report",
    "write_counting_bam",
]
