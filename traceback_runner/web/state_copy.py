"""Plain-language copy for every state token the local site shows (usability C3).

One row per enum value: a short ``label`` and a one-sentence ``meaning``.  The
site renders these words; the raw tokens appear only inside the collapsed
"exact values" disclosure.  This module is the one source of UI state copy:
``web/records.py`` attaches rows to each ``LocalRecordView`` and the jobs
disclosure uses :data:`JOB_STATE_COPY`.

The copy is descriptive.  Nothing here qualifies a method or makes a record fit
for clinical use; every string passes the public-text boundary
(``validate_public_text``), so it carries no paths, identifiers or URIs.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from evidence_inspector.method_registry import DisplayRole
from evidence_inspector.result_catalog import CatalogQualificationState
from evidence_inspector.result_catalog import TrustState as CatalogTrustState
from traceback_runner.contracts import JobState, PreflightOutcome
from traceback_runner.export import ReferenceMatch

#: Display-role token for a capability with no role assigned.
NOT_ASSIGNED: Final = "not_assigned"
#: Preflight token when the run's report cannot be read from the job store.
PREFLIGHT_NOT_AVAILABLE: Final = "not_available"
#: Comparison token for a record shown on its own.
NOT_COMPARED: Final = "not_compared"

Copy = tuple[str, str]  # (label, meaning)

_QUALIFICATION: dict[str, Copy] = {
    CatalogQualificationState.DEVELOPMENT_UNQUALIFIED.value: (
        "Not qualified",
        "A development measurement, not checked against any approved method.",
    ),
    CatalogQualificationState.UNKNOWN.value: (
        "Qualification unknown",
        "The method authority records no qualification for this method.",
    ),
    CatalogQualificationState.QUALIFIED.value: (
        "Qualified by the method authority",
        "The method authority records a qualification for this method. "
        "Records made on this workstation never carry it.",
    ),
}

_TRUST: dict[str, Copy] = {
    CatalogTrustState.DEVELOPMENT_SIGNATURE_VERIFIED.value: (
        "Signature verified (development key)",
        "The record's files match their signed checksums, signed by this "
        "workstation's development key. This shows the files are unchanged "
        "since signing; it is not an outside approval.",
    ),
}

_DISPLAY_ROLE: dict[str, Copy] = {
    DisplayRole.RESEARCH_BASELINE.value: (
        "Research baseline",
        "Shown for local research inspection only; never used for a provider "
        "or clinical decision.",
    ),
    DisplayRole.RESEARCH_CHALLENGER.value: (
        "Research challenger",
        "A research method shown beside a baseline, for inspection only.",
    ),
    DisplayRole.PROVIDER_PRIMARY.value: (
        "Provider display",
        "The method authority marks this method for provider display. Records "
        "made on this workstation never carry this role.",
    ),
    DisplayRole.DISABLED.value: (
        "Display disabled",
        "The method authority has disabled display of this method.",
    ),
    NOT_ASSIGNED: (
        "No display role",
        "The method authority assigns no display role to this method.",
    ),
}

_REFERENCE_MATCH: dict[str, Copy] = {
    "registered_digests": (
        "Matched by contig checksums",
        "Every contig in the BAM header carried a checksum (M5) that matched "
        "the registered reference.",
    ),
    "name_and_length_only": (
        "Matched by contig name and length only",
        "The BAM header carried no contig checksums (M5), so the reference was "
        "matched by contig names and lengths. A different assembly with the "
        "same names and lengths would not be noticed.",
    ),
}

_PREFLIGHT: dict[str, Copy] = {
    PreflightOutcome.PASS.value: (
        "Preflight passed",
        "Every input check passed.",
    ),
    PreflightOutcome.WARN.value: (
        "Preflight passed with warnings",
        "The measurement ran; some input checks raised warnings, listed below.",
    ),
    PreflightOutcome.PARTIAL.value: (
        "Preflight partly checked",
        "Some input checks could not be completed; the measurement ran on what "
        "could be checked. The checks are listed below.",
    ),
    PreflightOutcome.BLOCKED.value: (
        "Preflight blocked",
        "An input check failed, so no measurement is expected from this input.",
    ),
    PREFLIGHT_NOT_AVAILABLE: (
        "Preflight details unavailable",
        "The run's preflight report could not be read from this ROOT's job "
        "store. The signed record still states how the reference was matched.",
    ),
}

_COMPARISON: dict[str, Copy] = {
    NOT_COMPARED: (
        "Shown on its own",
        "This record is not compared with any other. To view two records, "
        "select them in the catalog.",
    ),
    "comparable": (
        "Comparable under a registered decision",
        "A registered compatibility decision allows these results to be shown "
        "together.",
    ),
    "incompatible": (
        "Not comparable",
        "A registered compatibility decision says these results measure "
        "different things; they are not shown together.",
    ),
    "unknown": (
        "Comparability unknown",
        "No registered compatibility decision covers this pair, so no "
        "differences are computed.",
    ),
}

_RECORD_STATUS: dict[str, Copy] = {
    "verified": (
        "Verified",
        "The signed files and the method authority were re-checked for this "
        "page.",
    ),
    "failed_verification": (
        "Failed verification",
        "The record's files or its method authority did not check out. Nothing "
        "from it is shown. Run traceback verify on it.",
    ),
    "view_unavailable": (
        "View unavailable",
        "The catalog row has no valid explorer view. Import the record again "
        "with traceback catalog import.",
    ),
}

_JOB: dict[str, Copy] = {
    JobState.DISCOVERED.value: ("Found", "The input was found; nothing has run yet."),
    JobState.WAITING_FOR_FINALIZATION.value: (
        "Waiting for the input to finish",
        "The input is still being written; the job waits.",
    ),
    JobState.SNAPSHOTTING.value: (
        "Copying the input",
        "A sealed copy of the input is being made.",
    ),
    JobState.VALIDATING.value: ("Checking the input", "Input checks are running."),
    JobState.READY.value: ("Ready", "The input is sealed and checked; the job can start."),
    JobState.QUEUED.value: ("Queued", "The job is waiting to start."),
    JobState.RUNNING.value: ("Running", "The job is running."),
    JobState.BASECALLING.value: ("Basecalling", "The basecalling stage is running."),
    JobState.ALIGNING.value: ("Aligning", "The alignment stage is running."),
    JobState.SORTING_INDEXING.value: (
        "Sorting and indexing",
        "The sort and index stage is running.",
    ),
    JobState.TECHNICAL_QC.value: ("Technical checks", "Technical checks are running."),
    JobState.MEASURING.value: ("Measuring", "The fragment measurement is running."),
    JobState.PAUSE_REQUESTED.value: (
        "Pausing",
        "A pause was requested; the job stops after its current stage.",
    ),
    JobState.PAUSED.value: ("Paused", "The job is paused; resume it to continue."),
    JobState.VALIDATING_OUTPUT.value: (
        "Checking the output",
        "The measurement output is being checked.",
    ),
    JobState.SIGNING.value: (
        "Signing",
        "The record is being signed with the development key.",
    ),
    JobState.COMPLETE.value: (
        "Finished",
        "The job finished and its signed record was written.",
    ),
    JobState.RETRYABLE_FAILURE.value: (
        "Failed; can be retried",
        "The job stopped on a problem that a retry may get past.",
    ),
    JobState.TERMINAL_FAILURE.value: (
        "Failed",
        "The job stopped on a problem a retry cannot change.",
    ),
    JobState.CANCELLED.value: ("Cancelled", "The job was cancelled."),
    JobState.SUPERSEDED.value: ("Superseded", "A newer job replaced this one."),
}

#: Axis name -> token -> (label, meaning).  Read-only.
STATE_COPY: Final = MappingProxyType(
    {
        "qualification": MappingProxyType(_QUALIFICATION),
        "trust": MappingProxyType(_TRUST),
        "display_role": MappingProxyType(_DISPLAY_ROLE),
        "reference_match": MappingProxyType(_REFERENCE_MATCH),
        "preflight": MappingProxyType(_PREFLIGHT),
        "comparison": MappingProxyType(_COMPARISON),
        "record_status": MappingProxyType(_RECORD_STATUS),
        "job": MappingProxyType(_JOB),
    }
)

#: Axes in the order the "What this record is" table shows them.
RECORD_AXES: Final = (
    "qualification",
    "trust",
    "display_role",
    "reference_match",
    "preflight",
    "comparison",
)

#: Every enum whose members the site can show, by axis (C3 coverage test).
ENUM_SOURCES: Final = MappingProxyType(
    {
        "qualification": tuple(item.value for item in CatalogQualificationState),
        "trust": tuple(item.value for item in CatalogTrustState),
        "display_role": (*(item.value for item in DisplayRole), NOT_ASSIGNED),
        "reference_match": ReferenceMatch.__args__,  # type: ignore[attr-defined]
        "preflight": (*(item.value for item in PreflightOutcome), PREFLIGHT_NOT_AVAILABLE),
        "comparison": (NOT_COMPARED, "comparable", "incompatible", "unknown"),
        "job": tuple(item.value for item in JobState),
    }
)


def copy_for(axis: str, token: str) -> Copy:
    """Return ``(label, meaning)``; ``KeyError`` for an unknown axis or token."""

    return STATE_COPY[axis][token]


__all__ = [
    "ENUM_SOURCES",
    "NOT_ASSIGNED",
    "NOT_COMPARED",
    "PREFLIGHT_NOT_AVAILABLE",
    "RECORD_AXES",
    "STATE_COPY",
    "copy_for",
]
