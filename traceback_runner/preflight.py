"""Privacy-safe, unqualified preflight for runner-owned BAM snapshots."""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
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


def _reference_matches(
    header: dict[str, Any],
    registered: RegisteredReference,
    *,
    compare_assembly: bool = True,
) -> PreflightOutcome:
    """Compare ``@SQ`` provenance with a registration.

    Count, order, ``SN`` and ``LN`` must match exactly. Any present ``M5`` or
    compared ``AS`` that differs blocks. When every line carries matching
    ``M5`` and ``AS`` (and ``AS`` is compared) the header passes; otherwise the
    names and lengths match but sequence identity is not bound by the header,
    which is a warning.
    """

    sequences = header.get("SQ")
    if not isinstance(sequences, list) or len(sequences) != len(registered.contigs):
        return PreflightOutcome.BLOCKED
    complete = compare_assembly
    for observed, expected in zip(sequences, registered.contigs, strict=True):
        if not isinstance(observed, dict) or (
            observed.get("SN") != expected.name or observed.get("LN") != expected.length
        ):
            return PreflightOutcome.BLOCKED
        md5 = observed.get("M5")
        if md5 is None:
            complete = False
        elif str(md5).lower() != expected.md5:
            return PreflightOutcome.BLOCKED
        assembly = observed.get("AS")
        if assembly is None:
            complete = False
        elif compare_assembly and assembly != registered.assembly:
            return PreflightOutcome.BLOCKED
    return PreflightOutcome.PASS if complete else PreflightOutcome.WARN


_DIFF_SHOWN = 3
_NAME_SHOWN = 64


def _sq_pairs(header: dict[str, Any]) -> list[tuple[str, object]]:
    sequences = header.get("SQ")
    if not isinstance(sequences, list):
        return []
    return [
        (str(item.get("SN", "")), item.get("LN")) if isinstance(item, dict) else ("", None)
        for item in sequences
    ]


def _chr_rename_direction(
    observed: list[tuple[str, object]], expected: list[tuple[str, int]]
) -> str | None:
    """``"strip"``/``"add"`` when the lists differ only by a ``chr`` name prefix."""

    if len(observed) != len(expected):
        return None
    directions = set()
    for (name, length), (reference_name, reference_length) in zip(
        observed, expected, strict=True
    ):
        if length != reference_length:
            return None
        if name == reference_name:
            continue
        if name == f"chr{reference_name}":
            directions.add("strip")
        elif f"chr{name}" == reference_name:
            directions.add("add")
        else:
            return None
    return directions.pop() if len(directions) == 1 else None


def _reference_diff(header: dict[str, Any], registered: RegisteredReference) -> tuple[str, str]:
    """Problem and fix for a BLOCKED TBX-BAM-002, comparing ``@SQ`` by position.

    Lists the first three differing positions as
    ``position name_in_BAM length_in_BAM | name_in_reference length_in_reference``
    (``-`` on the absent side) and the total count. Contig names come from
    the reference and the BAM header, never from reads or input locators.
    """

    observed = _sq_pairs(header)
    expected = [(contig.name, contig.length) for contig in registered.contigs]
    rows: list[str] = []
    differing = 0
    for position in range(max(len(observed), len(expected))):
        left = observed[position] if position < len(observed) else None
        right = expected[position] if position < len(expected) else None
        if left == right:
            continue
        differing += 1
        if len(rows) < _DIFF_SHOWN:
            bam_side = "- -" if left is None else f"{left[0][:_NAME_SHOWN]} {left[1]}"
            ref_side = "- -" if right is None else f"{right[0][:_NAME_SHOWN]} {right[1]}"
            rows.append(f"{position + 1} {bam_side} | {ref_side}")
    if not differing:
        # Names and lengths agree, so a present M5 or AS differs.
        return (
            "BAM @SQ names and lengths match the registered reference, but a "
            "recorded M5 (sequence digest) or AS (assembly) differs.",
            "The BAM was likely aligned to a different build with the same contig "
            "names; register that FASTA or realign against the registered one.",
        )
    shown = "; ".join(rows)
    problem = (
        f"BAM @SQ lines differ from the registered reference at {differing} of "
        f"{max(len(observed), len(expected))} positions (BAM {len(observed)} contigs, "
        f"reference {len(expected)}). First differences (position name_in_BAM "
        f"length_in_BAM | name_in_reference length_in_reference): {shown}."
    )
    direction = _chr_rename_direction(observed, expected)
    if direction == "strip":
        fix = (
            "Only the names differ (a chr prefix); rename with `samtools reheader`, "
            "for example: samtools view -H IN.bam | sed -E 's/SN:chr/SN:/' | "
            "samtools reheader - IN.bam > OUT.bam (then samtools index OUT.bam)."
        )
    elif direction == "add":
        fix = (
            "Only the names differ (a chr prefix); rename with `samtools reheader`, "
            "for example: samtools view -H IN.bam | sed -E 's/SN:/SN:chr/' | "
            "samtools reheader - IN.bam > OUT.bam (then samtools index OUT.bam)."
        )
    else:
        fix = (
            "This BAM was aligned to a different reference; register that FASTA "
            "or realign against the registered one."
        )
    return problem, fix


ALIGNMENT_FASTA_PLACEHOLDER = "REF.fa"


def alignment_command(fasta: str = ALIGNMENT_FASTA_PLACEHOLDER) -> str:
    """The minimap2 + samtools command that aligns an unaligned ONT BAM.

    ``-T MM,ML,MN`` and ``-y`` carry the modification tags through
    alignment. Traceback prints this command; it never aligns for the
    operator (alignment choices are scientific decisions).
    """

    return (
        "samtools fastq -T MM,ML,MN IN.bam \\\n"
        f"  | minimap2 -ax map-ont -y {fasta} - \\\n"
        "  | samtools sort -o OUT.sorted.bam\n"
        "samtools index OUT.sorted.bam"
    )


def unaligned_remediation(fasta: str | None = None) -> str:
    """TBX-BAM-003's FIX; ``fasta`` (a shell-quoted path, human output only)
    replaces the ``REF.fa`` placeholder."""

    target = (
        "OUT.sorted.bam:"
        if fasta is not None
        else f"OUT.sorted.bam, with {ALIGNMENT_FASTA_PLACEHOLDER} the registered FASTA:"
    )
    return (
        "Align it first (an assisted prerequisite, outside traceback; see "
        "'Aligning MinKNOW output' in docs/OPERATOR-GUIDE.md), then rerun on "
        f"{target}\n" + alignment_command(fasta or ALIGNMENT_FASTA_PLACEHOLDER)
    )


def _unaligned_check() -> PreflightCheck:
    return _check(
        "TBX-BAM-003",
        PreflightOutcome.BLOCKED,
        "This BAM is unaligned (no @SQ reference lines). MinKNOW and Dorado "
        "write unaligned BAMs by default.",
        unaligned_remediation(),
    )


def _empty_check() -> PreflightCheck:
    return _check(
        "TBX-BAM-004",
        PreflightOutcome.BLOCKED,
        "BAM has no alignment records.",
        "This is often a `bam_fail` or empty chunk; use the sample's `bam_pass` files.",
    )


def read_bam_header(bam_path: str | Path) -> dict[str, Any]:
    """The BAM header as a dict, opened without requiring ``@SQ`` lines."""

    import pysam

    with pysam.AlignmentFile(str(bam_path), "rb", check_sq=False) as bam:
        return bam.header.to_dict()


def header_is_unaligned(header: dict[str, Any]) -> bool:
    """True when the header has no ``@SQ`` lines (MinKNOW/Dorado default)."""

    sequences = header.get("SQ")
    return not isinstance(sequences, list) or not sequences


def unaligned_report() -> PreflightReport:
    """The one-check BLOCKED report for an unaligned BAM (TBX-BAM-003)."""

    return _report([_unaligned_check()])


def intake_refusal(bam_path: str | Path) -> PreflightCheck | None:
    """TBX-BAM-003 (unaligned) or TBX-BAM-004 (no records) from a cheap peek.

    Reads the header and at most one record, so ``run`` can refuse before it
    creates a job or copies anything. ``None`` when neither applies or the
    file cannot be read here (the full preflight reports that as TBX-BAM-001).
    """

    import pysam

    try:
        with pysam.AlignmentFile(str(bam_path), "rb", check_sq=False) as bam:
            if header_is_unaligned(bam.header.to_dict()):
                return _unaligned_check()
            if next(iter(bam.fetch(until_eof=True)), None) is None:
                return _empty_check()
    except (OSError, ValueError):
        return None
    return None


_MODEL_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@+-"
)


def _declared_modbase_models(description: object) -> tuple[str, ...]:
    """``modbase_models=<id>[,<id>...]`` from one Dorado/MinKNOW ``@RG DS`` field."""

    if not isinstance(description, str):
        return ()
    models: list[str] = []
    for token in description.split():
        key, separator, value = token.partition("=")
        if separator and key == "modbase_models":
            models.extend(item for item in value.split(",") if item)
    return tuple(models)


def _printable_model(model_id: str) -> str | None:
    """The model ID when it is short and plain enough to echo in a report."""

    if 0 < len(model_id) <= 96 and set(model_id) <= _MODEL_ID_CHARACTERS:
        return model_id
    return None


_TRACEBACK_PG_DECLARATION = "traceback @PG DS"


def _model_declaration(header: dict[str, Any], model_id: str | None) -> str | None:
    """Where the header declares the modified-base model, or ``None``.

    Accepted: the traceback ``@PG DS`` (``traceback.modified_base_model=<id>``,
    synthetic fixtures) or any ``@RG DS`` carrying ``modbase_models=<id>``, the
    form Dorado and MinKNOW write. When the policy names a model, only that
    model counts.
    """

    if model_id is not None and any(
        isinstance(program, dict)
        and program.get("DS") == f"traceback.modified_base_model={model_id}"
        for program in header.get("PG", [])
    ):
        return _TRACEBACK_PG_DECLARATION
    for group in header.get("RG", []):
        if not isinstance(group, dict):
            continue
        declared = _declared_modbase_models(group.get("DS"))
        if model_id is not None:
            declared = tuple(item for item in declared if item == model_id)
        if declared:
            shown = _printable_model(declared[0])
            return (
                f"model {shown} declared by @RG DS"
                if shown is not None
                else "model declared by @RG DS"
            )
    return None


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


def _same_bytes(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while left_chunk := left_handle.read(1024 * 1024):
            if left_chunk != right_handle.read(len(left_chunk)):
                return False
        return right_handle.read(1) == b""


def _index_matches_bam(bam_path: str, index_path: str, pysam: Any) -> bool:
    """Rebuild the supplied BAI/CSI format and compare its exact index bytes."""

    supplied = Path(index_path)
    with supplied.open("rb") as handle:
        prefix = handle.read(12)
    if prefix[:4] == b"BAI\x01":
        arguments = ()
        suffix = ".bai"
    else:
        try:
            with pysam.BGZFile(str(supplied), "rb") as handle:
                prefix = handle.read(12)
        except (OSError, ValueError):
            return False
    if prefix[:4] == b"CSI\x01" and len(prefix) == 12:
        min_shift = struct.unpack("<i", prefix[4:8])[0]
        if not 1 <= min_shift <= 31:
            return False
        arguments = ("-c", "-m", str(min_shift))
        suffix = ".csi"
    elif prefix[:4] != b"BAI\x01":
        return False
    with tempfile.TemporaryDirectory(prefix="traceback-index-check-") as directory:
        rebuilt = Path(directory) / f"rebuilt{suffix}"
        _rebuild_index(*arguments, "-o", str(rebuilt), bam_path)
        return _same_bytes(supplied, rebuilt)


def _rebuild_index(*arguments: str) -> None:
    """Run ``samtools index`` in a child interpreter.

    ``pysam.index`` runs samtools in-process and holds the GIL for the whole
    rebuild (about 65 s for a 2 GB BAM), which starves the stage heartbeat
    thread so the runner's 30 s worker lease expires mid-validate. A child
    process leaves the GIL free. Any failure raises, which callers treat as a
    contradictory index.
    """

    subprocess.run(
        [sys.executable, "-c", "import sys, pysam; pysam.index(*sys.argv[1:])", *arguments],
        check=True,
        capture_output=True,
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
    *,
    compare_assembly: bool = True,
) -> PreflightReport:
    """Validate a sealed BAM and matching index without exposing locators.

    ``compare_assembly=False`` is for registrations made without an assembly
    name: ``AS`` is then never compared, so the header can at best WARN.

    An unaligned BAM (no ``@SQ``) returns one BLOCKED TBX-BAM-003 check and a
    BAM with no records one BLOCKED TBX-BAM-004 check. Only read and format
    errors (``OSError``, ``ValueError``, a failed ``samtools quickcheck``) map
    to TBX-BAM-001; anything else propagates to the caller.
    """

    import pysam
    from pysam.utils import SamtoolsError

    bam_locator = str(bam_path)
    index_locator = str(index_path) if index_path is not None else None
    checks: list[PreflightCheck] = []
    try:
        # ``-u`` accepts a header without @SQ, so TBX-BAM-003 can say so.
        pysam.quickcheck("-u", bam_locator)
        checks.append(
            _check(
                "TBX-BAM-001",
                PreflightOutcome.PASS,
                "BAM snapshot is readable and complete.",
                "No action required.",
            )
        )
        with pysam.AlignmentFile(bam_locator, "rb", check_sq=False) as bam:
            header = bam.header.to_dict()
            if header_is_unaligned(header):
                return unaligned_report()
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
                if not _index_matches_bam(bam_locator, index_locator, pysam):
                    raise ValueError("index does not correspond to BAM")
            except Exception:
                checks.append(
                    _check(
                        "TBX-BAM-001",
                        PreflightOutcome.BLOCKED,
                        "BAM index is missing, unreadable, or contradictory.",
                        "Regenerate the index from the sealed BAM snapshot.",
                    )
                )

            reference_outcome = _reference_matches(
                header, registered_reference, compare_assembly=compare_assembly
            )
            if reference_outcome == PreflightOutcome.PASS:
                checks.append(
                    _check(
                        "TBX-BAM-002",
                        PreflightOutcome.PASS,
                        "BAM header matches the registered reference.",
                        "No action required.",
                    )
                )
            elif reference_outcome == PreflightOutcome.WARN:
                checks.append(
                    _check(
                        "TBX-BAM-002",
                        PreflightOutcome.WARN,
                        "BAM header lacks M5/AS; contig names and lengths match "
                        "the registered reference.",
                        "Optional: `samtools reheader` with M5/AS for full provenance.",
                    )
                )
            else:
                problem, fix = _reference_diff(header, registered_reference)
                checks.append(
                    _check("TBX-BAM-002", PreflightOutcome.BLOCKED, problem, fix)
                )

            saw_tagged = saw_invalid = False
            actual_sorted = True
            previous: tuple[int, int] | None = None
            saw_unplaced = False
            observed_mapped = observed_unmapped = sampled = 0
            for record in bam.fetch(until_eof=True):
                if record.is_unmapped:
                    observed_unmapped += 1
                else:
                    observed_mapped += 1
                is_unplaced = record.reference_id < 0 or record.reference_start < 0
                if is_unplaced:
                    saw_unplaced = True
                else:
                    coordinate = (record.reference_id, record.reference_start)
                    if saw_unplaced or (previous is not None and coordinate < previous):
                        actual_sorted = False
                    previous = coordinate
                if record.is_unmapped or record.is_secondary or record.is_supplementary:
                    continue
                if sampled < policy.modification_records_to_sample:
                    sampled += 1
                    tagged, valid = _modification_tags_valid(record)
                    saw_tagged |= tagged
                    saw_invalid |= tagged and not valid

            if observed_mapped + observed_unmapped == 0:
                return _report([_empty_check()])

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

            checks.append(
                _modification_check(
                    _model_declaration(header, policy.modified_base_model_id),
                    sampled=sampled,
                    saw_tagged=saw_tagged,
                    saw_invalid=saw_invalid,
                )
            )
    except (OSError, ValueError, SamtoolsError):
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


def _modification_check(
    declaration: str | None, *, sampled: int, saw_tagged: bool, saw_invalid: bool
) -> PreflightCheck:
    """TBX-MOD-001/002 from sampled tags and the header's model declaration.

    "Re-basecall" is advised only when tags are absent or contradictory:
    valid MM/ML/MN without a declared model (every Dorado BAM after a
    ``samtools fastq | minimap2`` alignment drops ``@RG``) is a WARN.
    """

    if saw_invalid:
        return _check(
            "TBX-MOD-002",
            PreflightOutcome.PARTIAL,
            "Sampled modification tags are structurally contradictory.",
            "Re-basecall for future methylation work; fragment measurement may continue.",
        )
    if sampled == 0 or not saw_tagged:
        return _check(
            "TBX-MOD-001",
            PreflightOutcome.PARTIAL,
            # Wording kept byte-identical (pinned synthetic preflight digests).
            "Modification provenance or sampled MM/ML/MN tags are absent.",
            "Re-basecall for future methylation work; fragment measurement may continue.",
        )
    if declaration is None:
        return _check(
            "TBX-MOD-001",
            PreflightOutcome.WARN,
            "Modification tags present; basecall model not declared in the header.",
            "No action needed for fragment length.",
        )
    if declaration == _TRACEBACK_PG_DECLARATION:
        # Synthetic fixtures; wording kept byte-identical (pinned digests).
        problem = "Modification provenance and sampled tags are compatible."
    else:
        problem = f"Modification tags present; {declaration}."
    return _check("TBX-MOD-001", PreflightOutcome.PASS, problem, "No action required.")
