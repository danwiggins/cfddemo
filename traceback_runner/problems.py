"""One table of operator problem text: CAUSE and FIX for every ``TBX-*`` code.

``traceback status`` and ``traceback logs`` read a failed job's stored
``last_error`` (``CODE: summary``) and look the code up here, so a failure
recorded hours ago still explains itself.  ``tests/test_problem_table.py``
asserts that every code literal in ``traceback_runner/`` and
``evidence_inspector/`` has a row here and a row in the operator guide.

Nothing here is a qualification or clinical statement; every local output
stays unqualified, local and not for clinical use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProblemText:
    """The stable operator explanation of one code."""

    cause: str
    fix: str


# A stored failure reason whose summary may be shown: it starts with a code.
# Anything else (an uncoded exception, which may name a file) is never shown.
CODED_FAILURE = re.compile(r"^(TBX-[A-Z]+(?:-[A-Z]+)?-?[0-9]{0,3}):\s?(.*)$", re.DOTALL)

UNCODED_SUMMARY = "uncoded failure; see traceback support-bundle"

PROBLEM_TABLE: dict[str, ProblemText] = {
    # References
    "TBX-REF-001": ProblemText(
        "The FASTA is missing, gzip-compressed, has no .fai, or its .fai contradicts it",
        "Decompress it, run samtools faidx REF.fa, then register again",
    ),
    "TBX-REF-002": ProblemText(
        "A different FASTA is already registered under this reference ID",
        "Keep the existing registration, or register under a new --id",
    ),
    "TBX-REF-003": ProblemText(
        "The reference ID is not registered under this ROOT, or its registration is damaged",
        "Run traceback reference register first (check --root)",
    ),
    # BAM inspection
    "TBX-BAM-001": ProblemText(
        "The BAM or its index is unreadable, truncated, not coordinate-sorted, or "
        "contradicts the other",
        "samtools sort, then samtools index, and run again",
    ),
    "TBX-BAM-002": ProblemText(
        "A contig name, length, order, M5 or AS differs from the registered reference",
        "Realign against the registered FASTA, or register the FASTA the BAM was aligned to",
    ),
    "TBX-BAM-003": ProblemText(
        "The BAM is unaligned (no @SQ lines); MinKNOW and Dorado write unaligned BAMs "
        "by default",
        "Align it with the printed minimap2 command (see Aligning MinKNOW output), then "
        "preflight the sorted output",
    ),
    "TBX-BAM-004": ProblemText(
        "The BAM has a header but no alignment records",
        "This is often a bam_fail or empty chunk; use the sample's bam_pass files",
    ),
    "TBX-REF-004": ProblemText(
        "preflight ran without --reference on a ROOT that has registered references",
        "Add --reference ID; the problem lists the registered IDs",
    ),
    "TBX-INTERNAL-001": ProblemText(
        "Preflight stopped on an unexpected internal error, not a BAM read or format error",
        "Retrying will not change it; write traceback support-bundle and report the code",
    ),
    "TBX-MOD-001": ProblemText(
        "No modification provenance or MM/ML tags were found",
        "No action needed for fragment length",
    ),
    "TBX-MOD-002": ProblemText(
        "Sampled modification tags are structurally contradictory",
        "No action needed for fragment length",
    ),
    # traceback run
    "TBX-RUN-003": ProblemText(
        "traceback run was called without --reference",
        "Register the FASTA, then pass --reference ID; traceback demo is the synthetic workflow",
    ),
    "TBX-RUN-004": ProblemText(
        "ROOT's volume has less free space than 2x the input, or filled during the run",
        "Free space or use a --root on a larger volume, then run again",
    ),
    "TBX-RUN-005": ProblemText(
        "No alignment passed the locked policy, so there is no eligible denominator",
        "Check contig names against the policy contigs, MAPQ 20, and the duplicate, "
        "secondary, supplementary and QC-fail flags",
    ),
    "TBX-RUN-006": ProblemText(
        "ROOT/trust/provenance-hmac.key is not a private 32-byte file",
        "Restore ROOT/trust/provenance-hmac.key from a backup (mode 0600), or use a fresh ROOT",
    ),
    "TBX-RUN-007": ProblemText(
        "ROOT/trust/development-local-signing.key is not a private 32-byte file",
        "Restore it from a backup (mode 0600), or use a fresh ROOT",
    ),
    "TBX-RUN-008": ProblemText(
        "The BAM is missing, is a symbolic link, or is not a regular file",
        "Check the BAM path; pass the file itself, not a link or a directory",
    ),
    "TBX-RUN-009": ProblemText(
        "The BAM index is missing (default: BAM.bai beside the BAM)",
        "samtools index BAM, or pass --index",
    ),
    "TBX-RUN-010": ProblemText(
        "The input does not start with the BGZF bytes every BAM starts with",
        "This is not a BAM; for FASTQ or POD5 see Aligning MinKNOW output in the guide",
    ),
    # Jobs
    "TBX-JOB-001": ProblemText(
        "The local run stopped without a record for an unexpected reason, or lost its "
        "worker lease",
        "traceback status JOB_ID and traceback logs JOB_ID; fix the stated cause, then "
        "traceback resume JOB_ID",
    ),
    "TBX-JOB-002": ProblemText(
        "Another traceback process holds this job's worker lease",
        "Wait for it, or check traceback status JOB_ID",
    ),
    # Catalog and local authority
    "TBX-CAT-001": ProblemText(
        "The path or ID is not a verifiable local record under this ROOT",
        "Pass a record ID from traceback catalog list, and check it with traceback verify",
    ),
    "TBX-CAT-002": ProblemText(
        "The registered reference ID fails the explorer's public-text rules",
        "Register the FASTA again under a neutral ID (for example hg38) and run again",
    ),
    "TBX-CAT-003": ProblemText(
        "The catalog export target file already exists",
        "Choose a new --csv file name, or move the existing file",
    ),
    "TBX-AUTH-LOCAL-001": ProblemText(
        "ROOT/authority is missing, not private, or a store fails its pinned check",
        "Restore ROOT/authority from a backup, or remove ROOT/authority and ROOT/catalog "
        "together and import the records again",
    ),
    "TBX-AUTH-LOCAL-002": ProblemText(
        "ROOT/trust/result-trust-registry or its pin is missing or does not open",
        "Remove the registry and its .pin.json, then import again",
    ),
    # traceback serve
    "TBX-SERVE-001": ProblemText(
        "No runner database under ROOT (wrong --root, or no run yet)",
        "Run traceback run first, or pass the right --root",
    ),
    "TBX-SERVE-002": ProblemText(
        "No catalog under ROOT: no record has been imported",
        "traceback catalog import RECORD_ID --root ROOT first",
    ),
    "TBX-SERVE-003": ProblemText(
        "The local web service could not start its listener",
        "Use the running service, or stop it and start again",
    ),
    "TBX-SERVE-004": ProblemText(
        "The local web service stopped itself after a security check failed",
        "Restart traceback serve --root ROOT",
    ),
    # Browser sessions (local web service)
    "TBX-AUTH-001": ProblemText(
        "The browser session expired, was idle too long, or was logged out",
        "Press Enter in serve's terminal for a fresh link",
    ),
    "TBX-AUTH-002": ProblemText(
        "A changing request carried no valid CSRF token",
        "Reload the page from a fresh serve link",
    ),
    "TBX-AUTH-003": ProblemText(
        "The request's host, origin or path is not the local service's own",
        "Open the link serve printed, on this machine, without a proxy",
    ),
    "TBX-AUTH-004": ProblemText(
        "Too many browser sessions are active",
        "Log out of an old tab, or restart serve",
    ),
    "TBX-AUTH-005": ProblemText(
        "Too many wrong launch links were tried",
        "Wait a minute, then use a fresh link from serve",
    ),
    "TBX-AUTH-006": ProblemText(
        "This reader session is already bound to a reader grant",
        "Ask the operator for a new reader launch link",
    ),
    "TBX-AUTH-007": ProblemText(
        "A reader session asked for an operator route",
        "Use the operator link that serve prints",
    ),
    "TBX-WEB-400": ProblemText(
        "The local web service refused a malformed request",
        "Reload the page; correct the selection",
    ),
    "TBX-WEB-404": ProblemText(
        "The requested page, record or route does not exist",
        "Return to the catalog and select again",
    ),
    "TBX-WEB-431": ProblemText(
        "The request headers were too large",
        "Reload the page; clear this site's cookies if it repeats",
    ),
    "TBX-WEB-503": ProblemText(
        "The local web service is busy or a store is temporarily unavailable",
        "Retry in a few seconds",
    ),
    "TBX-INTERNAL": ProblemText(
        "An internal error occurred; nothing was changed",
        "Retry; if it repeats, write a support bundle and report it",
    ),
    "TBX-OUT-001": ProblemText(
        "An output contained a value the privacy rules forbid",
        "Remove the forbidden value",
    ),
}


def failure_block(last_error: str | None) -> dict[str, str | None] | None:
    """The ``failure`` block of ``status`` and ``logs`` for a stored reason.

    Only a reason that starts with a code is shown; an uncoded reason (which
    may carry a file name) is replaced by a fixed pointer to the support bundle.
    """

    if last_error is None:
        return None
    match = CODED_FAILURE.match(last_error)
    if match is None:
        return {"code": None, "summary": UNCODED_SUMMARY, "cause": None, "fix": None}
    code, summary = match.group(1), match.group(2).strip()
    text = PROBLEM_TABLE.get(code)
    return {
        "code": code,
        "summary": summary,
        "cause": text.cause if text else None,
        "fix": text.fix if text else None,
    }


__all__ = ["CODED_FAILURE", "PROBLEM_TABLE", "ProblemText", "UNCODED_SUMMARY", "failure_block"]
