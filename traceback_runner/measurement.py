"""Complete deterministic aligned reference-span scan for sealed BAM snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, computed_field, model_validator

from traceback_runner.contracts import (
    FRAGMENT_POLICY_V1,
    FRAGMENT_POLICY_V2,
    AnyFragmentMeasurement,
    AnyFragmentMeasurementPolicy,
    ApprovalState,
    CompletionState,
    ExclusionCounts,
    FragmentMeasurement,
    FragmentMeasurementPolicy,
    FragmentMeasurementPolicyV2,
    FragmentMeasurementV2,
    HistogramCount,
    RunnerContract,
)
from traceback_runner.serialization import canonical_json_bytes

REFERENCE_CONSUMING_CIGAR_CODES = frozenset({0, 2, 3, 7, 8})


class ScanCompletion(StrEnum):
    COMPLETE = "complete"
    CAPPED = "capped"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ExclusionReason(StrEnum):
    UNMAPPED = "unmapped"
    SECONDARY = "secondary"
    SUPPLEMENTARY = "supplementary"
    QC_FAILURE = "qc_failure"
    DUPLICATE = "duplicate"
    LOW_MAPPING_QUALITY = "low_mapping_quality"
    UNREGISTERED_CONTIG = "unregistered_contig"
    NO_REFERENCE_SPAN = "no_reference_span"


class MeasurementScan(RunnerContract):
    """Aggregate scan state, including explicitly non-publishable outcomes.

    The scan carries its policy's schema version and approval label, so
    ``finalize_measurement`` builds the matching measurement class and never
    falls back to the synthetic default.  Scans are never persisted.
    """

    # v2 added policy_schema_version and approval_state (scans are in-memory
    # only; no v1 scan bytes were ever persisted).
    schema_version: Annotated[
        str, StringConstraints(pattern=r"^traceback\.measurement-scan\.v2$")
    ] = "traceback.measurement-scan.v2"
    policy_schema_version: Literal[
        "traceback.fragment-policy.v1", "traceback.fragment-policy.v2"
    ]
    approval_state: ApprovalState
    definition_id: str
    reference_id: str
    completion: ScanCompletion
    records_scanned: int = Field(ge=0)
    eligible_alignments: int = Field(ge=0)
    exclusions: ExclusionCounts
    histogram: tuple[HistogramCount, ...] = Field(min_length=1)

    @computed_field
    @property
    def publishable(self) -> bool:
        return self.completion == ScanCompletion.COMPLETE and self.eligible_alignments > 0

    @model_validator(mode="after")
    def reconcile_counts(self) -> MeasurementScan:
        if (
            self.policy_schema_version == FRAGMENT_POLICY_V1
            and self.approval_state != ApprovalState.UNAPPROVED_SYNTHETIC
        ):
            raise ValueError("a v1 policy scan is synthetic only")
        if self.records_scanned != self.eligible_alignments + self.exclusions.total:
            raise ValueError("every scanned record must be eligible or excluded")
        if sum(item.count for item in self.histogram) != self.eligible_alignments:
            raise ValueError("histogram counts must equal the eligible denominator")
        return self


class MeasurementUnavailableError(RuntimeError):
    """Raised when a partial or empty scan is requested as a measurement."""


def _exclusion(record: Any, policy: AnyFragmentMeasurementPolicy) -> ExclusionReason | None:
    if record.is_unmapped:
        return ExclusionReason.UNMAPPED
    if record.is_secondary:
        return ExclusionReason.SECONDARY
    if record.is_supplementary:
        return ExclusionReason.SUPPLEMENTARY
    if record.is_qcfail:
        return ExclusionReason.QC_FAILURE
    if record.is_duplicate:
        return ExclusionReason.DUPLICATE
    if record.reference_name not in policy.contigs:
        return ExclusionReason.UNREGISTERED_CONTIG
    if record.mapping_quality < policy.min_mapping_quality:
        return ExclusionReason.LOW_MAPPING_QUALITY
    if record.cigartuples is None:
        return ExclusionReason.NO_REFERENCE_SPAN
    return None


def _reference_span(record: Any) -> int:
    return sum(
        length
        for operation, length in record.cigartuples
        if operation in REFERENCE_CONSUMING_CIGAR_CODES
    )


def _histogram(
    spans: Counter[int], policy: AnyFragmentMeasurementPolicy
) -> tuple[HistogramCount, ...]:
    counts = [0] * len(policy.bins)
    for span, count in spans.items():
        for index, bin_definition in enumerate(policy.bins):
            if span < bin_definition.lower_inclusive:
                continue
            if bin_definition.upper_exclusive is None or span < bin_definition.upper_exclusive:
                counts[index] += count
                break
    return tuple(
        HistogramCount(bin=bin_definition, count=counts[index])
        for index, bin_definition in enumerate(policy.bins)
    )


def _build_scan(
    policy: AnyFragmentMeasurementPolicy,
    completion: ScanCompletion,
    inspected: int,
    exclusions: Counter[ExclusionReason],
    spans: Counter[int],
) -> MeasurementScan:
    exclusion_counts = ExclusionCounts(
        **{reason.value: exclusions[reason] for reason in ExclusionReason}
    )
    if type(policy) not in (FragmentMeasurementPolicy, FragmentMeasurementPolicyV2):
        raise TypeError("fragment measurement policy must be an exact policy contract")
    return MeasurementScan(
        policy_schema_version=policy.schema_version,
        approval_state=policy.approval_state,
        definition_id=policy.definition_id,
        reference_id=policy.reference_id,
        completion=completion,
        records_scanned=inspected,
        eligible_alignments=sum(spans.values()),
        exclusions=exclusion_counts,
        histogram=_histogram(spans, policy),
    )


def scan_records(
    records: Iterable[Any],
    policy: AnyFragmentMeasurementPolicy,
    *,
    maximum_records: int | None = None,
    should_interrupt: Callable[[int], bool] | None = None,
) -> MeasurementScan:
    """Scan records once, retaining canonical integer aggregates only."""

    if maximum_records is not None and maximum_records < 1:
        raise ValueError("maximum_records must be positive when provided")
    exclusions: Counter[ExclusionReason] = Counter()
    spans: Counter[int] = Counter()
    inspected = 0
    completion = ScanCompletion.COMPLETE
    iterator = iter(records)
    while True:
        try:
            if should_interrupt is not None and should_interrupt(inspected):
                completion = ScanCompletion.INTERRUPTED
                break
            if maximum_records is not None and inspected >= maximum_records:
                try:
                    next(iterator)
                except StopIteration:
                    break
                completion = ScanCompletion.CAPPED
                break
            try:
                record = next(iterator)
            except StopIteration:
                break
            inspected += 1
            try:
                reason = _exclusion(record, policy)
                if reason is not None:
                    exclusions[reason] += 1
                    continue
                span = _reference_span(record)
                if span <= 0 or span > 2**63 - 1:
                    exclusions[ExclusionReason.NO_REFERENCE_SPAN] += 1
                else:
                    spans[span] += 1
            except Exception:
                exclusions[ExclusionReason.NO_REFERENCE_SPAN] += 1
                completion = ScanCompletion.FAILED
                break
        except Exception:
            completion = ScanCompletion.FAILED
            break
    return _build_scan(policy, completion, inspected, exclusions, spans)


def scan_aligned_reference_spans(
    bam_path: str | Path,
    policy: AnyFragmentMeasurementPolicy,
    *,
    maximum_records: int | None = None,
    should_interrupt: Callable[[int], bool] | None = None,
) -> MeasurementScan:
    """Scan one preflight-approved sealed BAM snapshot to end-of-file."""

    import pysam

    try:
        with pysam.AlignmentFile(str(bam_path), "rb", check_sq=True) as bam:
            return scan_records(
                bam.fetch(until_eof=True),
                policy,
                maximum_records=maximum_records,
                should_interrupt=should_interrupt,
            )
    except Exception:
        return _build_scan(policy, ScanCompletion.FAILED, 0, Counter(), Counter())


def finalize_measurement(scan: MeasurementScan) -> AnyFragmentMeasurement:
    """Construct the shared publishable aggregate from one complete scan.

    A v1 policy scan finalizes into ``FragmentMeasurement`` (synthetic only);
    a v2 policy scan into ``FragmentMeasurementV2`` with the policy's approval
    label.
    """

    if type(scan) is not MeasurementScan:
        raise TypeError("finalize_measurement requires an exact MeasurementScan")
    # Revalidate: model_copy(update=...) skips validators, so a caller could
    # otherwise cross the policy version and approval label.
    scan = MeasurementScan.model_validate(scan.model_dump(mode="python", exclude={"publishable"}))
    if not scan.publishable:
        raise MeasurementUnavailableError(
            "measurement is unavailable because the complete eligible denominator was not observed"
        )
    if scan.policy_schema_version == FRAGMENT_POLICY_V2:
        return FragmentMeasurementV2(
            definition_id=scan.definition_id,
            approval_state=scan.approval_state,
            reference_id=scan.reference_id,
            completion=CompletionState.COMPLETE,
            records_scanned=scan.records_scanned,
            eligible_alignments=scan.eligible_alignments,
            exclusions=scan.exclusions,
            histogram=scan.histogram,
        )
    return FragmentMeasurement(
        definition_id=scan.definition_id,
        reference_id=scan.reference_id,
        completion=CompletionState.COMPLETE,
        records_scanned=scan.records_scanned,
        eligible_alignments=scan.eligible_alignments,
        exclusions=scan.exclusions,
        histogram=scan.histogram,
    )


def canonical_measurement_bytes(scan: MeasurementScan) -> bytes:
    """Return canonical shared measurement JSON, rejecting partial scans."""

    return canonical_json_bytes(finalize_measurement(scan))


def chart_data(scan: MeasurementScan) -> tuple[dict[str, int | None], ...]:
    """Return deterministic chart rows only for a publishable scan."""

    measurement = finalize_measurement(scan)
    return tuple(
        {
            "lower_inclusive": item.bin.lower_inclusive,
            "upper_exclusive": item.bin.upper_exclusive,
            "count": item.count,
        }
        for item in measurement.histogram
    )
