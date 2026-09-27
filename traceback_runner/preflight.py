"""Privacy-safe preflight for runner-owned synthetic BAM snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from traceback_runner.contracts import (
    Identifier,
    PreflightCheck,
    PreflightOutcome,
    PreflightReport,
    RegisteredReference,
    RunnerContract,
    StageName,
)


class BamPreflightPolicy(RunnerContract):
    """Synthetic-only validation settings; not a scientific approval."""

    policy_id: Identifier
    modification_records_to_sample: int = Field(default=100, ge=1, le=10_000)
    modified_base_model_id: Identifier | None = None


def _check(code: str, outcome: PreflightOutcome, problem: str, fix: str) -> PreflightCheck:
    return PreflightCheck(
        code=code,
        outcome=outcome,
        problem=problem,
        likely_cause=problem,
        remediation=fix,
        owner="workflow operator",
        stage=StageName.VALIDATE,
        retryable=False,
        documentation_path="/docs/MEASUREMENT-CONTRACT",
        supporting_artifact_role="analysis_bam",
    )


def _reference_matches(header: dict[str, Any], registered: RegisteredReference) -> bool:
    sequences = header.get("SQ")
    if not isinstance(sequences, list) or len(sequences) != len(registered.contigs):
        return False
    for observed, expected in zip(sequences, registered.contigs, strict=True):
        if not isinstance(observed, dict) or (
            observed.get("SN") != expected.name
            or observed.get("LN") != expected.length
            or observed.get("AS") != registered.assembly
            or str(observed.get("M5", "")).lower() != expected.md5
        ):
            return False
    return True


def _header_has_model(header: dict[str, Any], model_id: str | None) -> bool:
    declaration = f"traceback.modified_base_model={model_id}"
    return model_id is not None and any(
        isinstance(program, dict) and program.get("DS") == declaration
        for program in header.get("PG", [])
    )


def _modification_tags_valid(record: Any) -> tuple[bool, bool]:
    present = tuple(record.has_tag(tag) for tag in ("MM", "ML", "MN"))
    if not any(present):
        return False, False
    if not all(present):
        return True, False
    try:
        mm = record.get_tag("MM")
        ml = record.get_tag("ML")
        mn = record.get_tag("MN")
        parsed = record.modified_bases
        call_count = sum(len(calls) for calls in parsed.values())
    except (KeyError, TypeError, ValueError, AttributeError):
        return True, False
    return True, (
        isinstance(mm, str)
        and bool(mm)
        and isinstance(mn, int)
        and record.query_length is not None
        and mn == record.query_length
        and parsed is not None
        and call_count == len(ml)
    )


def _report(checks: list[PreflightCheck]) -> PreflightReport:
    severity = {
        PreflightOutcome.PASS: 0,
        PreflightOutcome.WARN: 1,
        PreflightOutcome.PARTIAL: 2,
        PreflightOutcome.BLOCKED: 3,
    }
    outcome = max((check.outcome for check in checks), key=severity.__getitem__)
    blocked = outcome == PreflightOutcome.BLOCKED
    mod_failed = any(
        check.code in {"TBX-MOD-001", "TBX-MOD-002"}
        and check.outcome != PreflightOutcome.PASS
        for check in checks
    )
    return PreflightReport(
        outcome=outcome,
        fragment_measurement_eligible=not blocked,
        future_methylation_eligible=not blocked and not mod_failed,
        checks=tuple(checks),
    )


def validate_bam_snapshot(
    bam_path: str | Path,
    index_path: str | Path | None,
    registered_reference: RegisteredReference,
    policy: BamPreflightPolicy,
) -> PreflightReport:
    """Validate a sealed BAM and matching index without exposing locators."""

    import pysam

    bam_locator = str(bam_path)
    index_locator = str(index_path) if index_path is not None else None
    checks: list[PreflightCheck] = []
    try:
        pysam.quickcheck(bam_locator)
        checks.append(
            _check(
                "TBX-BAM-001",
                PreflightOutcome.PASS,
                "BAM snapshot is readable and complete.",
                "No action required.",
            )
        )
        with pysam.AlignmentFile(bam_locator, "rb", check_sq=True) as bam:
            header = bam.header.to_dict()
            index_counts: tuple[int, int] | None = None
            try:
                if index_locator is None:
                    raise OSError("index was not supplied")
                with pysam.AlignmentFile(
                    bam_locator,
                    "rb",
                    index_filename=index_locator,
                    check_sq=True,
                ) as indexed:
                    indexed.check_index()
                    stats = indexed.get_index_statistics()
                    index_counts = (
                        sum(item.mapped for item in stats),
                        sum(item.unmapped for item in stats) + indexed.nocoordinate,
                    )
            except Exception:
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.BLOCKED,
                        "BAM index is missing, unreadable, or contradictory.",
                        "Regenerate the index from the sealed BAM snapshot.",
                    )
                )

            if _reference_matches(header, registered_reference):
                checks.append(
                    _check(
                        "TBX-BAM-002",
                        PreflightOutcome.PASS,
                        "BAM header matches the registered reference.",
                        "No action required.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "TBX-BAM-002",
                        PreflightOutcome.BLOCKED,
                        "BAM reference provenance does not match the registered asset.",
                        "Realign against the registered reference asset.",
                    )
                )

            saw_tagged = saw_invalid = False
            actual_sorted = True
            previous: tuple[int, int] | None = None
            saw_unmapped = False
            observed_mapped = observed_unmapped = sampled = 0
            for record in bam.fetch(until_eof=True):
                if record.is_unmapped:
                    saw_unmapped = True
                    observed_unmapped += 1
                else:
                    observed_mapped += 1
                    coordinate = (record.reference_id, record.reference_start)
                    if saw_unmapped or (previous is not None and coordinate < previous):
                        actual_sorted = False
                    previous = coordinate
                if record.is_unmapped or record.is_secondary or record.is_supplementary:
                    continue
                if sampled < policy.modification_records_to_sample:
                    sampled += 1
                    tagged, valid = _modification_tags_valid(record)
                    saw_tagged |= tagged
                    saw_invalid |= tagged and not valid

            if index_counts == (observed_mapped, observed_unmapped):
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.PASS,
                        "BAM index reconciles with the complete record scan.",
                        "No action required.",
                    )
                )
            elif index_counts is not None:
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.BLOCKED,
                        "BAM index totals contradict the sealed BAM snapshot.",
                        "Regenerate the index from the sealed BAM snapshot.",
                    )
                )

            header_sorted = header.get("HD", {}).get("SO") == "coordinate"
            if header_sorted and actual_sorted:
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.PASS,
                        "BAM header and records prove coordinate sort order.",
                        "No action required.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.BLOCKED,
                        "BAM does not have proven coordinate sort order.",
                        "Coordinate-sort and re-index the BAM.",
                    )
                )

            model_present = _header_has_model(header, policy.modified_base_model_id)
            if saw_invalid:
                checks.append(
                    _check(
                        "TBX-MOD-002",
                        PreflightOutcome.PARTIAL,
                        "Sampled modification tags are structurally contradictory.",
                        "Re-basecall for future methylation work; "
                        "fragment measurement may continue.",
                    )
                )
            elif not model_present or sampled == 0 or not saw_tagged:
                checks.append(
                    _check(
                        "TBX-MOD-001",
                        PreflightOutcome.PARTIAL,
                        "Modification provenance or sampled MM/ML/MN tags are absent.",
                        "Re-basecall for future methylation work; "
                        "fragment measurement may continue.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "TBX-MOD-001",
                        PreflightOutcome.PASS,
                        "Modification provenance and sampled tags are compatible.",
                        "No action required.",
                    )
                )
    except Exception:
        checks = [
            _check(
                "TBX-BAM-001",
                PreflightOutcome.BLOCKED,
                "BAM snapshot is unreadable, truncated, or structurally invalid.",
                "Regenerate or recopy the BAM and index, then retry.",
            ),
            _check(
                "TBX-MOD-001",
                PreflightOutcome.PARTIAL,
                "Modification eligibility could not be established.",
                "Resolve the blocking BAM error before evaluating future methylation eligibility.",
            ),
        ]
    return _report(checks)
