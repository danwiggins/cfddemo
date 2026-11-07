"""samtools and minimap2: where they are, which versions, and the minimums (D9).

Aligning an unaligned input needs samtools >= 1.20 and minimap2 >= 2.28
(TBX-LAB-003, exit 3).  An already-aligned input needs neither for ingest.
Versions are recorded in the aligned input's identity (D31).

``resolve_aligner_tools`` prefers the pinned aligner store
(``traceback toolchain install aligners``; see ``aligner_toolchain``) and
falls back to ``PATH``.  A ``PATH`` tool has provenance ``unpinned``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

MIN_SAMTOOLS = (1, 20)
MIN_MINIMAP2 = (2, 28)
PINNED = "pinned"
UNPINNED = "unpinned"
_VERSION = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str | None
    # ``pinned``: from the verified aligner store; ``pin_sha256`` is its
    # release tarball digest.  ``unpinned``: whatever ``PATH`` resolved.
    provenance: str = UNPINNED
    pin_sha256: str | None = None

    @property
    def numeric(self) -> tuple[int, int] | None:
        if self.version is None:
            return None
        match = _VERSION.match(self.version)
        return (int(match.group(1)), int(match.group(2))) if match else None


def _version(argv: list[str], pattern: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - absolute executable, no shell
            argv, capture_output=True, text=True, errors="replace", timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(pattern, completed.stdout + completed.stderr)
    return match.group(1) if match and completed.returncode == 0 else None


def find_samtools() -> Tool:
    path = shutil.which("samtools")
    version = _version([path, "--version"], r"samtools (\d+\.\d+(?:\.\d+)?)") if path else None
    return Tool("samtools", path, version)


def find_minimap2() -> Tool:
    path = shutil.which("minimap2")
    version = _version([path, "--version"], r"^(\d+\.\d+[^\s]*)") if path else None
    return Tool("minimap2", path, version)


def resolve_aligner_tools() -> tuple[Tool, Tool]:
    """samtools and minimap2 from the verified pinned store, else ``PATH``.

    A store that is present but does not verify is TBX-LAB-033.  Reads the
    cache and hashes files only: never the network.
    """

    from .aligner_toolchain import resolve_aligners

    return resolve_aligners()


def provenance_of(samtools: Tool, minimap2: Tool) -> str:
    """``pinned`` only when both aligners come from the verified store."""

    pinned = all(getattr(tool, "provenance", UNPINNED) == PINNED for tool in (samtools, minimap2))
    return PINNED if pinned else UNPINNED


def pins_of(samtools: Tool, minimap2: Tool) -> dict[str, str] | None:
    """The ``tool_pins`` identity term: both tarball digests when pinned, else ``None``."""

    if provenance_of(samtools, minimap2) != PINNED:
        return None
    return {"samtools": str(samtools.pin_sha256), "minimap2": str(minimap2.pin_sha256)}


def aligner_provenance(samtools: Tool, minimap2: Tool) -> dict[str, object]:
    """Path-free provenance for the run log and the input manifest."""

    return {
        "provenance": provenance_of(samtools, minimap2),
        **{
            tool.name: {"version": tool.version, "provenance": tool.provenance, "pin_sha256": tool.pin_sha256}
            for tool in (samtools, minimap2)
        },
    }


def _ok(tool: Tool, minimum: tuple[int, int]) -> bool:
    return tool.path is not None and tool.numeric is not None and tool.numeric >= minimum


def require_aligner_tools(samtools: Tool | None = None, minimap2: Tool | None = None) -> tuple[Tool, Tool]:
    """Both tools at their minimums, or TBX-LAB-003."""

    if samtools is None or minimap2 is None:
        resolved_samtools, resolved_minimap2 = resolve_aligner_tools()
        samtools = samtools or resolved_samtools
        minimap2 = minimap2 or resolved_minimap2
    problems = []
    for tool, minimum in ((samtools, MIN_SAMTOOLS), (minimap2, MIN_MINIMAP2)):
        if not _ok(tool, minimum):
            found = tool.version or ("missing" if tool.path is None else "unreadable version")
            problems.append(f"{tool.name} {found} (needs >= {minimum[0]}.{minimum[1]})")
    if problems:
        from .problems import LabProblem

        missing = [tool.name for tool in (samtools, minimap2) if tool.path is None]
        fix = (
            f"brew install {' '.join(missing)}" if missing else "brew upgrade samtools minimap2"
        ) + ", then `traceback lab doctor`"
        raise LabProblem(
            "TBX-LAB-003",
            cause="unaligned input needs alignment: " + "; ".join(problems),
            fix=fix,
            data={
                "samtools": samtools.version,
                "minimap2": minimap2.version,
                "minimum": {"samtools": "1.20", "minimap2": "2.28"},
            },
        )
    return samtools, minimap2


__all__ = [
    "aligner_provenance",
    "pins_of",
    "provenance_of",
    "MIN_MINIMAP2",
    "MIN_SAMTOOLS",
    "PINNED",
    "UNPINNED",
    "Tool",
    "find_minimap2",
    "find_samtools",
    "require_aligner_tools",
    "resolve_aligner_tools",
]
