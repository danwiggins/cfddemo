"""D34 stderr scrubbing: nothing that names a donor or a host path is persisted.

Tool stderr often echoes file names, and kit file names often embed a donor's
name.  Before any stderr reaches ``result.json`` or the run log:

- ``ROOT`` becomes ``<ROOT>`` (so store-relative locations stay readable);
- every other absolute or home-relative path becomes ``<path>``;
- ``$HOME``, the login name and every input chunk's file stem become
  ``<redacted>``;
- each line is capped at 300 characters.

Only the scrubbed text is ever written.  ``--verbose`` may stream raw stderr to
the operator's own terminal; that text is never stored.
"""

from __future__ import annotations

import getpass
import os
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINE_CHARS = 300
ROOT_TOKEN = "<ROOT>"
PATH_TOKEN = "<path>"
REDACTED = "<redacted>"

# An absolute POSIX path or a ``~``-relative path, not preceded by a word
# character (so ``a/b`` and ``12/40`` stay intact) and not the store-relative
# tail of the ``<ROOT>`` token.  Delimiters such as ``<…>`` end a path but do
# not protect it.  Quoted paths with spaces are handled separately below.
_PATH = re.compile(r"(?<![\w.~/-])(?<!<ROOT>)(?:~|/)(?:[^\s'\"<>|;,:()\[\]{}]+)")
_QUOTED_PATH = re.compile(r"(['\"])(?:~|/)[^'\"\n]*\1")


def _login_name() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is fine
        return None
    return name or None


@dataclass(frozen=True)
class Scrubber:
    """Scrub stderr for one run: knows ROOT, home, login and chunk stems."""

    root: Path | None = None
    chunk_stems: Sequence[str] = ()
    home: str | None = field(default_factory=lambda: os.path.expanduser("~"))
    login: str | None = field(default_factory=_login_name)
    max_line: int = MAX_LINE_CHARS

    def _roots(self) -> list[str]:
        if self.root is None:
            return []
        forms = {str(self.root)}
        try:
            forms.add(os.path.realpath(self.root))
        except OSError:
            pass
        return sorted((form.rstrip("/") for form in forms if form not in {"", "/"}), key=len, reverse=True)

    def _secrets(self) -> list[str]:
        values = {stem for stem in self.chunk_stems if stem}
        if self.home and self.home not in {"", "/"}:
            values.add(self.home.rstrip("/"))
        if self.login and len(self.login) >= 2:
            values.add(self.login)
        return sorted(values, key=len, reverse=True)

    def line(self, text: str) -> str:
        """Scrub one line (no newline handling)."""

        out = text.replace("\r", "")
        for root in self._roots():
            out = out.replace(root, ROOT_TOKEN)
        out = _QUOTED_PATH.sub(lambda match: f"{match.group(1)}{PATH_TOKEN}{match.group(1)}", out)
        out = _PATH.sub(PATH_TOKEN, out)
        for secret in self._secrets():
            pattern = re.escape(secret)
            # Login names are words; stems and home are literal substrings.
            if secret == self.login:
                pattern = rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])"
            out = re.sub(pattern, REDACTED, out)
        if len(out) > self.max_line:
            out = out[: self.max_line - 1] + "…"
        return out

    def text(self, text: str) -> str:
        """Scrub multi-line text."""

        return "\n".join(self.line(line) for line in text.splitlines())

    def lines(self, lines: Iterable[str]) -> list[str]:
        return [self.line(line) for line in lines]


class BoundedStderr:
    """Keep the head and tail of a stream's lines, bounded in memory.

    Raw lines live only in memory; callers persist ``scrubbed()`` only.
    """

    def __init__(self, *, head: int = 20, tail: int = 50) -> None:
        self._head_limit = head
        self._head: list[str] = []
        self._tail: deque[str] = deque(maxlen=tail)
        self.dropped = 0
        self.total = 0

    def add(self, line: str) -> None:
        self.total += 1
        line = line.rstrip("\n")
        if len(self._head) < self._head_limit:
            self._head.append(line)
            return
        if len(self._tail) == self._tail.maxlen:
            self.dropped += 1
        self._tail.append(line)

    def raw_lines(self) -> list[str]:
        if self.dropped:
            return [*self._head, f"… {self.dropped} lines omitted …", *self._tail]
        return [*self._head, *self._tail]

    def scrubbed(self, scrubber: Scrubber, *, last: int | None = None) -> list[str]:
        lines = scrubber.lines(self.raw_lines())
        return lines[-last:] if last else lines


__all__ = [
    "BoundedStderr",
    "MAX_LINE_CHARS",
    "PATH_TOKEN",
    "REDACTED",
    "ROOT_TOKEN",
    "Scrubber",
]
