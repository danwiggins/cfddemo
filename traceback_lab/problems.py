"""``TBX-LAB-*`` operator problems over the signed CLI's ``RunProblem`` shape (D36).

Every Lab refusal carries the six operator fields the signed CLI uses: code,
summary, cause, fix, retryable and a docs anchor, plus measured ``data`` that
never holds a host path.  The anchors live in ``docs/LAB-GUIDE.md``; a test
keeps this table and the guide in step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from traceback_runner.cli import RunProblem, _local_envelope, _problem

LAB_GUIDE = "docs/LAB-GUIDE.md"

# Lab exit codes: DESIGN.md's table plus 8 (completed with failed algorithms).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_NOT_FOUND = 4
EXIT_RETRYABLE = 6
EXIT_INTERNAL = 7
EXIT_PARTIAL = 8


@dataclass(frozen=True)
class ProblemSpec:
    exit_code: int
    summary: str
    retryable: bool = False


# The spec's Operator CLI contract table.  Exit 0 rows are warnings.
PROBLEMS: dict[str, ProblemSpec] = {
    "TBX-LAB-001": ProblemSpec(EXIT_USAGE, "Unknown algorithm id in --only"),
    "TBX-LAB-002": ProblemSpec(EXIT_USAGE, "Bad --downsample or --tf value"),
    "TBX-LAB-003": ProblemSpec(
        EXIT_BLOCKED, "samtools or minimap2 is missing or too old for an unaligned input"
    ),
    "TBX-LAB-004": ProblemSpec(EXIT_BLOCKED, "No usable registered reference"),
    "TBX-LAB-005": ProblemSpec(
        EXIT_BLOCKED, "An input is a symlink, not a regular file, or unreadable"
    ),
    "TBX-LAB-006": ProblemSpec(EXIT_BLOCKED, "A chunk fails samtools quickcheck -u (truncated)"),
    "TBX-LAB-007": ProblemSpec(
        EXIT_BLOCKED, "Duplicate chunk content, or one chunk index with two contents"
    ),
    "TBX-LAB-008": ProblemSpec(EXIT_BLOCKED, "Chunks declare different @RG basecall models"),
    "TBX-LAB-009": ProblemSpec(
        EXIT_BLOCKED, "An aligned input's @SQ differs from the registered reference"
    ),
    "TBX-LAB-010": ProblemSpec(EXIT_BLOCKED, "Not enough free space", retryable=True),
    "TBX-LAB-011": ProblemSpec(EXIT_BLOCKED, "The volume filled up during a step", retryable=True),
    "TBX-LAB-012": ProblemSpec(
        EXIT_BLOCKED, "Another Lab writer is running", retryable=True
    ),
    "TBX-LAB-013": ProblemSpec(EXIT_BLOCKED, "Read-count reconciliation failed after alignment"),
    "TBX-LAB-014": ProblemSpec(
        EXIT_BLOCKED, "An alignment process failed or timed out", retryable=True
    ),
    "TBX-LAB-015": ProblemSpec(EXIT_RETRYABLE, "Interrupted", retryable=True),
    "TBX-LAB-016": ProblemSpec(EXIT_PARTIAL, "The run completed and one or more algorithms failed"),
    "TBX-LAB-017": ProblemSpec(EXIT_NOT_FOUND, "Not found in the Lab store"),
    "TBX-LAB-018": ProblemSpec(
        EXIT_BLOCKED, "The Lab store was written by a newer checkout"
    ),
    "TBX-LAB-019": ProblemSpec(EXIT_OK, "Aligner identity changed; the sample will be realigned"),
    "TBX-LAB-020": ProblemSpec(EXIT_INTERNAL, "Unexpected internal error"),
    "TBX-LAB-021": ProblemSpec(EXIT_BLOCKED, "An EXAMPLE (synthetic) sample cannot join a cohort"),
    "TBX-LAB-022": ProblemSpec(EXIT_BLOCKED, "Stored cohort metadata needs restoration"),
    "TBX-LAB-023": ProblemSpec(EXIT_BLOCKED, "Sample sex assertion conflicts or needs restoration"),
    "TBX-LAB-024": ProblemSpec(EXIT_BLOCKED, "Input platform is unknown, unsupported, or inconsistent"),
    "TBX-LAB-025": ProblemSpec(EXIT_OK, "Physical RAM is below the alignment recommendation"),
}


def docs_anchor(code: str) -> str:
    """The Lab guide's troubleshooting anchor for one ``TBX-LAB-*`` code."""

    return f"{LAB_GUIDE}#{code.lower()}"


class LabProblem(RunProblem):
    """A Lab refusal: ``RunProblem`` fields plus the Lab guide anchor."""

    def __init__(
        self,
        code: str,
        *,
        cause: str,
        fix: str,
        summary: str | None = None,
        data: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        spec = PROBLEMS.get(code)
        if spec is None:
            raise ValueError(f"unknown Lab problem code {code!r}")
        super().__init__(
            code,
            summary or spec.summary,
            cause=cause,
            fix=fix,
            exit_code=spec.exit_code if exit_code is None else exit_code,
            retryable=spec.retryable,
            data=data,
        )

    @property
    def docs(self) -> str:
        return docs_anchor(self.code)


_STATUS_BY_EXIT = {
    EXIT_OK: "warn",
    EXIT_USAGE: "usage",
    EXIT_BLOCKED: "blocked",
    EXIT_NOT_FOUND: "not_found",
    EXIT_RETRYABLE: "retryable_failure",
    EXIT_INTERNAL: "internal_error",
    EXIT_PARTIAL: "partial_failure",
}


def problem_payload(command: str, problem: LabProblem, *, root: str | None = None) -> dict[str, Any]:
    """The signed ``_problem`` envelope, re-anchored to the Lab guide (cli-result v2)."""

    payload = _local_envelope(_problem(command, problem))
    payload["status"] = _STATUS_BY_EXIT.get(problem.exit_code, "blocked")
    payload["data"]["docs"] = problem.docs
    if root is not None:
        payload["data"] = {"root": root, **payload["data"]}
    return payload


def problem_lines(problem: LabProblem) -> list[str]:
    """Human lines: CODE, CAUSE, FIX, RETRYABLE, DOCS (always in this order)."""

    return [
        f"CODE       {problem.code}",
        f"CAUSE      {problem.cause}",
        f"FIX        {problem.fix}",
        f"RETRYABLE  {'yes' if problem.retryable else 'no'}",
        f"DOCS       {problem.docs}",
    ]


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_INTERNAL",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_RETRYABLE",
    "EXIT_USAGE",
    "LAB_GUIDE",
    "LabProblem",
    "PROBLEMS",
    "ProblemSpec",
    "docs_anchor",
    "problem_lines",
    "problem_payload",
]
