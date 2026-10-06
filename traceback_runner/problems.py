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
        "preflight ran without --reference on a ROOT that has registered references, "
        "or whose ROOT/references could not be read",
        "Add --reference ID (the problem lists the registered IDs); if ROOT/references "
        "could not be read, check it with traceback doctor",
    ),
    # Method assets (traceback method-asset)
    "TBX-ASSET-001": ProblemText(
        "A different file (other bytes or another location) is already registered under "
        "this asset ID, or the ID is registered as another kind",
        "Keep the existing registration, or register the new file under a new --id",
    ),
    "TBX-ASSET-002": ProblemText(
        "A registered asset file changed since registration (its SHA-256 or size differs), "
        "or a job's own copy of it was changed",
        "Restore the original file; to use the new file, register it under a new --id",
    ),
    "TBX-ASSET-003": ProblemText(
        "The asset file is missing or unreadable, or a --from-dir directory lacks one of "
        "the three Loyfer files",
        "Restore the file at its registered location, or pass the right --file or --from-dir",
    ),
    "TBX-ASSET-004": ProblemText(
        "The asset ID is not registered under this ROOT, or its registration is damaged",
        "Run the traceback method-asset register command the problem prints (check --root)",
    ),
    "TBX-ASSET-005": ProblemText(
        "The analysis's own parser for this kind rejects the file, or a line or the file "
        "is implausibly large; nothing was registered. For ichor-pon only the envelope is "
        "checked; readRDS validates the panel when the analysis runs",
        "Pass the unmodified file of the stated --kind",
    ),
    "TBX-INTERNAL-001": ProblemText(
        "Preflight stopped on an unexpected internal error, not a BAM read or format error",
        "Retrying will not change it. From run: traceback support-bundle JOB_ID --output "
        "DIR, then report the code. From preflight (no job exists): report the code and "
        "the command you ran",
    ),
    "TBX-LABEL-001": ProblemText(
        "ROOT/labels is a symbolic link or a file, so no label can be written or shown",
        "Remove ROOT/labels (labels are unsigned notes) and set the label again",
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
    "TBX-JOB-003": ProblemText(
        "resume: the job was admitted under another method definition than the one "
        "ROOT resolves now (the reference, a tool, an asset or a setting changed)",
        "Run the input again with traceback run; the current method is a new job",
    ),
    "TBX-RUN-011": ProblemText(
        "run --analysis: this version of traceback has no stages for that analysis; "
        "no job was created for it, and the other analyses still ran",
        "Run the other analyses without it (for example --analysis fragment)",
    ),
    # Cell origin (run --analysis cell-origin)
    "TBX-METH-001": ProblemText(
        "Cell origin needs modification calls: the sampled MM/ML tags are absent or "
        "contradictory, or modkit could not extract calls from the BAM",
        "Basecall with a 5mC model, keep MM/ML through alignment (samtools fastq -T "
        "MM,ML,MN), and run again; the fragment analysis is unaffected",
    ),
    "TBX-METH-002": ProblemText(
        "No basecall model is declared: the aligned BAM's header has no @RG "
        "modbase_models= and run had no --modbase-model, or the declaration "
        "contradicts the header",
        "Copy the model from the unaligned BAM's @RG line and pass run --modbase-model ID",
    ),
    "TBX-METH-003": ProblemText(
        "The registered reference lacks contigs the Loyfer marker regions use",
        "Register and align against the hg38 FASTA the atlas uses (UCSC chr names)",
    ),
    "TBX-METH-004": ProblemText(
        "Too few marker fragments to estimate a mixture: classified fragments or "
        "observed markers fall below the locked floors, or the fit had no usable signal",
        "Sequence deeper or pool more input; the floors are locked method parameters",
    ),
    "TBX-METH-005": ProblemText(
        "The input exceeds a locked cell-origin cap (CpG calls or fragment-marker groups); "
        "a cap hit refuses the record, never a partial one",
        "The caps are locked method parameters; a larger input needs a new method version",
    ),
    "TBX-METH-006": ProblemText(
        "The mixture fit (NNLS) did not converge; no record was made",
        "Retrying will not change it; report the code with traceback support-bundle",
    ),
    "TBX-METH-007": ProblemText(
        "The cell-origin result failed one of its validation checks (schema, asset "
        "digests, markers against the atlas, U/X/M counts, normalized fractions, "
        "publication safety)",
        "Retrying will not change it; write traceback support-bundle JOB_ID and report it",
    ),
    # Pinned analysis tools (reason: missing, or wrong version or digest)
    "TBX-TOOL-001": ProblemText(
        "Missing: the pinned tool or micromamba is not installed, or the install "
        "failed. Wrong version or digest: the installed binary, its receipt or its "
        "package record does not match the pin",
        "Run traceback toolchain install modkit to see the plan, then add --yes; it "
        "replaces a damaged install",
    ),
    "TBX-TOOL-002": ProblemText(
        "Missing: the copy-number (ichorCNA) toolchain or micromamba is not installed, "
        "an install stopped part-way, or a download failed. Wrong version or digest: "
        "readCounter, Rscript, the driver, an installed file or a Bioconductor data "
        "package no longer matches the lock and the install receipt",
        "Run traceback toolchain install ichor to see the plan, then add --yes; it "
        "replaces a damaged install. Fragment length never needs it",
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
    "TBX-AUTH-LOCAL-003": ProblemText(
        "A store under ROOT/method-authority fails its pinned SHA-256, location or replay "
        "check, or ROOT/method-authority is not a private directory; only the records "
        "bound to a damaged store are hidden",
        "Restore the named store directory (or ROOT/method-authority) from a backup; "
        "never remove ROOT/authority for this code",
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
