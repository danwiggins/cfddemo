"""Operator labels for local records (A4b): unsigned notes under ROOT/labels.

A label is an operator note, not part of the signed record: it lives at
``ROOT/labels/<record_id>.json`` as ``{"label": str, "set_at": ISO-8601}``,
outside every bundle, so setting one never changes a signed byte.  Labels are
shown in human CLI output only; they never enter any ``--json`` output, logs,
bundles, exports or support bundles (DESIGN.md privacy).  Do not put donor
names or other identifiers in a label.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from .references import ReferenceProblem

LABELS_DIRECTORY = "labels"
MAX_LABEL_CHARACTERS = 80
_MAX_LABEL_FILE_BYTES = 4096
_RECORD_ID = re.compile(r"record-[0-9a-f]{24}")
LABEL_QUALIFIER = "operator note, not part of the signed record"


class LabelError(ValueError):
    """A label breaks the grammar; the message names the rule."""


def label_violation(text: object) -> str | None:
    """The one label grammar (A4b), shared by the CLI writer and the site reader
    (``web/records.py``): the rule ``text`` breaks, or ``None`` when valid.

    1-80 characters; no leading or trailing whitespace; no ``/`` or ``\\``; no
    NUL, control or other Unicode ``C*`` (control, format, private-use,
    unassigned) characters; and it must pass the web service's own public-text
    check, so a label never fails a route that shows it.
    """

    from .web.contracts import validate_public_text

    if type(text) is not str or not 1 <= len(text) <= MAX_LABEL_CHARACTERS:
        return f"a label must be 1-{MAX_LABEL_CHARACTERS} characters"
    if text != text.strip():
        return "a label cannot start or end with whitespace"
    if "/" in text or "\\" in text:
        return "a label cannot contain / or \\"
    if any(unicodedata.category(character)[0] == "C" for character in text):
        return "a label cannot contain NUL or control characters"
    try:
        validate_public_text(text)
    except ValueError as exc:
        return f"a label must pass the public-text rules: {exc}"
    return None


def validate_label(text: str) -> str:
    """Return ``text`` if it is a valid label, else raise naming the rule."""

    rule = label_violation(text)
    if rule is not None:
        raise LabelError(rule)
    return text


class LabelStoreProblem(ReferenceProblem):
    """``ROOT/labels`` is not a real directory; no label was written."""


def _require_label_directory(directory: Path) -> None:
    """Refuse a symlinked or non-directory ``ROOT/labels``, exactly as the
    site's reader (``web/records.py``) refuses to read one."""

    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise LabelStoreProblem(
            "TBX-LABEL-001",
            "ROOT/labels is not a real directory; no label was set",
            cause="ROOT/labels is a symbolic link or a file",
            fix="Remove ROOT/labels (labels are unsigned notes) and set the label again",
        )


def _label_path(root: Path, record_id: str) -> Path:
    if not _RECORD_ID.fullmatch(record_id):
        raise LabelError("labels name a local record ID (record-<24 hex>)")
    return root / LABELS_DIRECTORY / f"{record_id}.json"


def read_label(root: Path, record_id: str) -> str | None:
    """The record's label, or ``None`` when absent or unreadable.

    No-follow and bounded: a symlink, an oversized or malformed file, or a
    label that no longer passes the grammar shows no label.
    """

    try:
        path = _label_path(root, record_id)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
    except (OSError, LabelError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        content = os.read(descriptor, _MAX_LABEL_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(content) > _MAX_LABEL_FILE_BYTES:
        return None
    try:
        document = json.loads(content)
        label = document["label"]
        return validate_label(label)
    except (ValueError, KeyError, TypeError):
        return None


def write_label(root: Path, record_id: str, text: str) -> str | None:
    """Set or replace the label; return the previous label (last writer wins).

    Temporary file, fsync, ``os.replace``, mode 0600.  Nothing under
    ``ROOT/records`` is touched.
    """

    label = validate_label(text)
    path = _label_path(root, record_id)
    _require_label_directory(path.parent)
    previous = read_label(root, record_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_label_directory(path.parent)
    content = (
        json.dumps(
            {"label": label, "set_at": datetime.now(UTC).isoformat(timespec="seconds")},
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=f".{record_id}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return previous


__all__ = [
    "LABEL_QUALIFIER",
    "LabelError",
    "MAX_LABEL_CHARACTERS",
    "read_label",
    "validate_label",
    "write_label",
]
