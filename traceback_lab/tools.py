"""samtools and minimap2: where they are, which versions, and the minimums (D9).

Aligning an unaligned input needs samtools >= 1.20 and minimap2 >= 2.28
(TBX-LAB-003, exit 3).  An already-aligned input needs neither for ingest.
Versions are recorded in the aligned input's identity (D31).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

MIN_SAMTOOLS = (1, 20)
MIN_MINIMAP2 = (2, 28)
_VERSION = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str | None

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


def _ok(tool: Tool, minimum: tuple[int, int]) -> bool:
    return tool.path is not None and tool.numeric is not None and tool.numeric >= minimum


def require_aligner_tools(samtools: Tool | None = None, minimap2: Tool | None = None) -> tuple[Tool, Tool]:
    """Both tools at their minimums, or TBX-LAB-003."""

    samtools = samtools or find_samtools()
    minimap2 = minimap2 or find_minimap2()
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
    "MIN_MINIMAP2",
    "MIN_SAMTOOLS",
    "Tool",
    "find_minimap2",
    "find_samtools",
    "require_aligner_tools",
]
