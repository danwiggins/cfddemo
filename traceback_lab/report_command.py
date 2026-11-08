"""``traceback lab report --html ID12 [--out PATH]``: one self-contained HTML research report.

The file carries the same captured research report the Lab page's
**Export / print** panel produces, rendered server-side from the sample
projection ``traceback_lab.web.api.LabApi.sample`` serves (the read-time
interpretation engine runs inside that projection).  A scientist can open
or share the file without a running server: it has no scripts, no inline
event handlers and no external resources, so it renders with JavaScript
disabled.

``project_report`` is a port of ``lab-report.js``'s ``project()``: the
metric registry, labels, value kinds, flags and license terms come from the
interpretation library (the source the browser's audited tables are checked
against), and the API payload's own restriction metadata is only compared
with it.  No display formatting is re-implemented: the Lab's number
formatting lives in ``lab-format.js`` and has no Python equivalent, so the
file prints the exact recorded values (the "Exact API value" of the print
view).

Text handling has three classes.  Identifiers, hashes, numbers and
enumerations are validated by pattern and only HTML-escaped.  Curated repo
text (labels, the library's method catalogue, license copy, the CLIA
explainer) has ROOT and home replaced and is never filtered or word-redacted.
Library free text (confounders, next steps, references) also loses the login
and host name, and
is withheld when it looks like a path, a credential or a read name; a
withheld item is marked in place and in the limitations.  The same 5,000-row and 5 MiB limits as the browser export
apply; an over-limit report is refused (TBX-LAB-034) without writing a
partial file.
"""

from __future__ import annotations

import argparse
import errno
import functools
import getpass
import hashlib
import html
import json
import math
import os
import re
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .interpretation.engine import NO_REFERENCE_REASONS, NOT_INTERPRETABLE_REASONS, Library, load_library
from .problems import EXIT_OK, LabProblem
from .scrub import PATH_TOKEN, REDACTED, ROOT_TOKEN
from .store import LabStore

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
SCHEMA_VERSION = "2"
MIN_HOST_WORD = 3  # the same rule as ``lab support-bundle``

# Fixed copy, kept in step with lab-report.js by tests/lab/test_lab_report_html.py.
RESTRICTION_FLAG_TEXT = {
    "CLIA": "FLAG: CLIA — health-assessment labels on an individual sample",
    "FDA": "FLAG: FDA — cancer-detection claim if marketed.",
    "PRIVACY": "FLAG: PRIVACY — can reveal pregnancy or transplant status.",
    "LEGAL": "FLAG: LEGAL — possible method patents; no FTO review.",
    "PII": "FLAG: PII — file paths often embed donor names.",
}
VIRAL_PRIVACY_TEXT = "FLAG: PRIVACY — viral-read signals can reveal sensitive infection-related information."
CLINICAL_ADVICE_WARNING = "FLAG: CLIA — reads as clinical advice if a donor or clinician sees it."
INCOMPLETE_RESTRICTIONS = (
    "Some restriction metadata was absent, unknown, or inconsistent; "
    "only audited flags and license terms are shown."
)
WITHHELD_TEXT = (
    "Some descriptive text was withheld because it resembled a path, credential, long identifier "
    "or read sequence; each withheld item is marked where it would appear."
)
RESEARCH_MARKS = "RESEARCH USE ONLY · NOT CLIA VALIDATED · NOT A DIAGNOSIS"
# lab run --below-depth (kept in step with lab-report.js).
UNDERPOWERED_NOTE = "underpowered (ran below the method's depth floor with --below-depth); exploratory only, not a call."
UNDERPOWERED_BANNER = "Underpowered — below the method's depth floor; exploratory only"
FROZEN_FOOTER = (
    "Values in this file are frozen at export time and may differ from the live Lab page; "
    "open the Lab for the current recorded result."
)
FIXED_LIMITATIONS = (
    "Research use only; not for diagnosis or clinical decisions.",
    "Only visible registered scalar metrics are included; hidden processing steps, arrays, "
    "artifacts, and taxon detail are excluded.",
    "Stable input and provenance identifiers may be linkable to other local records.",
    "Recorded date kind distinguishes a recorded completion from latest input use; unknown "
    "means completion status could not be established. A date alone is not proof of a "
    "completed run.",
)

STATES = frozenset({
    "not_interpretable", "no_reference", "in_range", "above_typical", "markedly_above",
    "below_typical", "markedly_below",
})
SKIP_REASONS = frozenset({
    "depth", "derived_input", "disk", "missing_toolchain", "missing_asset", "no_pon",
    "protocol_mismatch", "invalid_asset", "asset_unavailable", "missing_channel",
    "low_support", "missing_tags", "example_reference", "input_unavailable", "platform",
    "unsupported_input", "platform_unverified", "superseded_method", "derived_not_supported",
    "missing_input", "input_kind", "reference", "upstream", "unqualified_support",
    "missing_gpu", "model_not_ready",
})
# lab-report.js's producer-unit vocabulary (kept equal by test).  Each metric's
# own producer unit is read from lab-report.js's audited table (``audited_units``).
UNITS = frozenset({
    "end-count difference", "fraction", "arms", "log2", "log2/GC", "phred", "reads",
    "copies", "reads per million", "ratio", "bp", "records", "x", "index", "fragments",
    "years", "z-score", "correlation", "relative", "sites", "bins", "segments", "genes",
    "profiles", "calls", "markers", "bytes", "estimated reads", "estimated reads/million",
    "taxa", "reads / 1M unmapped primaries",
})
# The categorical values lab-report.js accepts; any other category is omitted.
_CATEGORIES = {
    ("sex-check", "call"): ("male", "female", "indeterminate"),
    ("copy-number", "pon_policy"): ("none", "lab_loo_v1"),
}
_BOOLEAN_CATEGORIES = frozenset({("copy-number", "identifiable"), ("meth-tf", "identifiable")})

_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID12 = re.compile(r"^[0-9a-f]{12,64}$")
_ALGORITHM = re.compile(r"^[a-z0-9-]{1,40}$")
_METRIC = re.compile(r"^[a-z0-9_]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INSTANT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?Z$"
)
_TOOL = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_TOOL_VERSION = re.compile(r"^[A-Za-z0-9_.+:-]{1,40}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ATTEMPT = re.compile(r"^a-\d{8}T\d{12}-[0-9a-f]{8}$")
# Withholding heuristics for library free text.  Credential tokens are matched
# by their case-sensitive shape (``AKIA`` + 16, ``sk-`` + 16 at a word start)
# or as an assignment (``password=…``), so prose such as "risk-adjusted",
# "Slovakia", "trade secret" or "tokenized" passes.  Paths match anywhere
# that is not inside a word ("log2/GC", "and/or" pass): POSIX absolute and
# home paths of two or more segments, Windows drive paths and file URLs.
# Read names are sequences, long hexadecimal runs and hyphenated UUIDs.
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\\]")
_CREDENTIAL = re.compile(
    r"\bAKIA[0-9A-Z]{16}\b|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"
    r"|(?i:\b(?:password|passwd|secret|token|api[_ -]?key)\b\s*[:=])"
)
_PATH_LIKE = re.compile(
    r"(?<![\w.])(?:~(?=/)|/[A-Za-z0-9._-]+)(?:/[A-Za-z0-9._ -]+)+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?i:\bfile:/)"
)
_READ_NAME = re.compile(
    r"[ACGTN]{30,}|\b[0-9a-fA-F]{32,}\b"
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SCRUB_TOKENS = re.compile("(" + "|".join(re.escape(token) for token in (ROOT_TOKEN, PATH_TOKEN, REDACTED)) + ")")
_INLINE = re.compile(r"\*\*(.+?)\*\*|\[\[(.+?)\]\]|\[([^\]]+)\]\((https?:[^)]+)\)")


def _utcnow() -> str:
    """The capture instant (seconds precision, like the browser's ISO stamp)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _host_pattern(hostname: str | None) -> re.Pattern[str] | None:
    """The host name and its first label as case-insensitive whole words (``lab support-bundle``'s rule)."""

    if not hostname:
        return None
    words = {word for word in (hostname, hostname.split(".", 1)[0]) if len(word) >= MIN_HOST_WORD}
    if not words:
        return None
    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    # ``_`` counts as a word character so ``lab`` never rewrites ``lab_loo_v1``.
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])", re.IGNORECASE)


@dataclass(frozen=True)
class ReportScrubber:
    """Redaction for the two text classes the file carries.

    Both classes have every literal form of ROOT replaced by ``<ROOT>`` and
    the home directory by ``<redacted>``.  Curated repo text is public
    content and gets nothing else, so a host or login called ``clia`` or
    ``root`` cannot rewrite it.  Library free text that looks like a path is
    withheld before this runs; what remains also loses the login name and
    the host name as whole words, outside the ``<ROOT>``/``<path>``/
    ``<redacted>`` tokens.  The signed scrubber's generic path rule is not
    used: it rewrites prose such as "5'/3' ends".
    """

    roots: tuple[str, ...] = ()
    home: str | None = None
    login: str | None = None
    host: re.Pattern[str] | None = None

    def _literal(self, text: str) -> str:
        """Replace ROOT and home only as whole path prefixes: ``/data/lab`` never matches
        ``/data/lab2`` and ``/data`` never matches ``input/data``."""

        for prefix, token in [*((root, ROOT_TOKEN) for root in self.roots), *([(self.home, REDACTED)] if self.home else [])]:
            text = re.sub(rf"(?<![A-Za-z0-9._~/-]){re.escape(prefix)}(?=/|$|[^A-Za-z0-9._~-])", token, text)
        return text

    def curated(self, text: str) -> str:
        """Scrub repo-curated text, without truncation or content filtering."""

        return self._literal(text)

    def redact_words(self, text: str) -> str:
        """Login and host-name redaction outside the scrubber tokens."""

        patterns = [pattern for pattern in (
            re.compile(rf"(?<![A-Za-z0-9_]){re.escape(self.login)}(?![A-Za-z0-9_])") if self.login else None,
            self.host,
        ) if pattern is not None]
        if not patterns:
            return text
        parts = _SCRUB_TOKENS.split(text)
        for index in range(0, len(parts), 2):
            for pattern in patterns:
                parts[index] = pattern.sub(REDACTED, parts[index])
        return "".join(parts)

    def free_text(self, value: Any) -> str | None:
        """Scrubbed library free text, or ``None`` when it must be withheld."""

        if not isinstance(value, str) or not value:
            return None
        if (_UNSAFE_CONTROL.search(value) or _CREDENTIAL.search(value) or _READ_NAME.search(value)
                or _PATH_LIKE.search(value)):
            return None
        return self.redact_words(self._literal(value)) or None


def _login() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is fine
        return None
    return name if name and len(name) >= 2 else None


def report_scrubber(root: Path) -> ReportScrubber:
    """The report's scrubber for one ROOT (host words of three or more characters)."""

    forms = {str(root)}
    try:
        forms.add(os.path.realpath(root))
    except OSError:
        pass
    roots = tuple(sorted((form.rstrip("/") for form in forms if form not in ("", "/")), key=len, reverse=True))
    home = os.path.expanduser("~").rstrip("/")
    return ReportScrubber(roots=roots, home=home if home and home != "~" else None,
                          login=_login(), host=_host_pattern(_hostname()))


class ReportRefused(Exception):
    """A report that cannot be built; ``cause`` becomes the TBX-LAB-034 CAUSE line."""

    def __init__(self, cause: str, **data: Any) -> None:
        super().__init__(cause)
        self.cause = cause
        self.data = data


def _instant(value: Any, pattern: re.Pattern[str] = _INSTANT) -> bool:
    """A real calendar instant in one of the forms ``pattern`` (and the browser) accepts."""

    if not isinstance(value, str) or not pattern.match(value):
        return False
    match = _INSTANT.match(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(part) if part else 0 for part in match.groups())
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return True


def _withheld(count: int) -> str:
    return f"[{count} item{'s' if count != 1 else ''} withheld]"


class _Notes:
    """The report's ordered, de-duplicated limitations."""

    def __init__(self) -> None:
        self.items: list[str] = list(FIXED_LIMITATIONS)

    def __call__(self, message: str) -> None:
        if message not in self.items:
            self.items.append(message)


def _free_text(value: Any, scrub: ReportScrubber, note: _Notes) -> str | None:
    if value is None or value == "" or value == []:
        return None
    items = value if isinstance(value, list) else [value]
    kept = [text for text in (scrub.free_text(item) for item in items) if text]
    withheld = len(items) - len(kept)
    if withheld:
        note(WITHHELD_TEXT)
        kept.append(_withheld(withheld))
    return "; ".join(kept)


def _curated(value: Any, scrub: ReportScrubber) -> str | None:
    if value is None or value == "" or value == [] or value == ():
        return None
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    kept = [scrub.curated(item) for item in items if isinstance(item, str) and item]
    if len(kept) != len(items):
        kept.append(f"[{len(items) - len(kept)} malformed item{'s' if len(items) - len(kept) != 1 else ''} omitted]")
    return "; ".join(kept)


def _valid_provenance(value: Any, sample: dict[str, Any]) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("tool"), str) and bool(_TOOL.match(value["tool"]))
        and isinstance(value.get("tool_version"), str) and bool(_TOOL_VERSION.match(value["tool_version"]))
        and isinstance(value.get("parameters_sha256"), str) and bool(_SHA.match(value["parameters_sha256"]))
        and value.get("source_id") == sample["source_id"]
        and value.get("input_id") == sample["full_input_id"]
        and isinstance(value.get("reference_id"), str) and bool(_REFERENCE.match(value["reference_id"]))
        and _instant(value.get("timestamp_utc"), _UTC)
        and (value.get("attempt_id") is None
             or (isinstance(value["attempt_id"], str) and bool(_ATTEMPT.match(value["attempt_id"]))))
    )


def _scalar(value: Any, algorithm_id: str, metric_id: str, value_kind: str | None) -> tuple[bool, Any, bool]:
    """(valid, value, nonfinite) for one recorded metric value, as lab-report.js's ``scalar``."""

    if value is None:
        return True, None, False
    if value_kind == "categorical":
        if (algorithm_id, metric_id) in _BOOLEAN_CATEGORIES:
            return isinstance(value, bool), value, False
        return isinstance(value, str) and value in _CATEGORIES.get((algorithm_id, metric_id), ()), value, False
    if isinstance(value, bool):
        return False, None, False
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return True, None, True
        return True, value, False
    return False, None, False


def _same_ids(actual: Any, expected: list[str]) -> bool:
    return (isinstance(actual, list) and len(actual) == len(expected)
            and len(set(map(str, actual))) == len(expected) and all(item in actual for item in expected))


def _restrictions(library: Library, algorithm_id: str, metric_id: str,
                  algorithm_version: Any) -> tuple[list[str], list[str]]:
    """Audited (flag ids, license ids), with lab-report.js's stored cell-origin v1 modkit rule."""

    entry = library.entry(algorithm_id, metric_id)
    flags = list(entry.get("flags") or []) if entry else []
    licenses = list(entry.get("license_ids") or []) if entry else []
    if algorithm_id == "cell-origin" and algorithm_version in ("", "1") and "modkit-ont-public" not in licenses:
        licenses.append("modkit-ont-public")
    return flags, licenses


_REPORT_JS = Path(__file__).resolve().parent / "web" / "static" / "lab-report.js"


@functools.lru_cache(maxsize=4)
def _audited_units(path: Path = _REPORT_JS, mtime_ns: int = 0) -> dict[tuple[str, str], str | None]:
    """Each metric's producer unit from lab-report.js's audited table: the browser's own source.

    The table is ``{algorithm: {metric: [label, producer unit, value kind]}}``;
    tests/lab/test_lab_report.py checks it against the algorithm producers.
    """

    del mtime_ns  # part of the cache key only
    source = path.read_text(encoding="utf-8")
    table = json.loads(source.split("  const audited = ", 1)[1].split(";\n  // Fixed, audited restrictions", 1)[0])
    return {(algorithm, metric): definition[1]
            for algorithm, metrics in table.items() for metric, definition in metrics.items()}


def audited_units() -> dict[tuple[str, str], str | None]:
    try:
        return _audited_units(_REPORT_JS, _REPORT_JS.stat().st_mtime_ns)
    except (OSError, IndexError, ValueError, TypeError, AttributeError, KeyError) as error:
        raise ReportRefused(f"the audited producer units could not be read ({type(error).__name__})") from None


def _visible_algorithms(library: Library) -> set[str]:
    from .registry import load_registry

    registered = {row.id for row in load_registry()}
    return {algorithm for algorithm, _ in library.entries
            if algorithm in registered and algorithm not in library.hidden_algorithms}


def project_report(sample: dict[str, Any], generated_at: str, scrub: ReportScrubber,
                   library: Library | None = None) -> dict[str, Any]:
    """The captured research report (ResearchReportV2 identity, limitations, rows, context).

    Mirrors ``lab-report.js``'s ``project()`` over the same API payload, with
    the interpretation library in place of the browser's audited tables.
    Raises ``ReportRefused`` when the sample cannot be reported or the row
    limit is exceeded.
    """

    library = library if library is not None else load_library()
    if not isinstance(sample, dict) or sample.get("found") is not True:
        raise ReportRefused("the sample is not in the Lab store")
    if sample.get("state"):
        raise ReportRefused("the sample's recorded result is unavailable", state=str(sample["state"])[:64])
    id12 = sample.get("id12")
    if (
        not isinstance(id12, str) or not _ID12.match(id12)
        or not isinstance(sample.get("source_id"), str) or not _SHA.match(sample["source_id"])
        or not sample["source_id"].startswith(id12)
        or not isinstance(sample.get("full_input_id"), str) or not _SHA.match(sample["full_input_id"])
        or not isinstance(sample.get("example"), bool)
        or not isinstance(sample.get("interpretation_version"), str)
        or not _VERSION.match(sample["interpretation_version"])
        or not (sample.get("cohort_version") is None
                or (isinstance(sample["cohort_version"], str) and _VERSION.match(sample["cohort_version"])))
        or not (sample.get("run_utc") == "" or _instant(sample.get("run_utc")))
        or not _instant(generated_at)
        or not isinstance(sample.get("catalog"), list) or not isinstance(sample.get("algorithms"), list)
    ):
        raise ReportRefused("the sample's identity or completion metadata is invalid; it cannot be reported")
    recorded_kind = "unknown"
    if "completed_utc" in sample:
        completed = sample["completed_utc"]
        if completed is None:
            recorded_kind = "input_use" if sample["run_utc"] else "unknown"
        elif _instant(completed) and completed == sample["run_utc"]:
            recorded_kind = "completion"
        else:
            raise ReportRefused("the sample has inconsistent completion metadata; it cannot be reported")

    note = _Notes()
    supplied: set[str] = set()
    if not isinstance(sample.get("licenses"), list):
        note(INCOMPLETE_RESTRICTIONS)
    else:
        for license in sample["licenses"]:
            license_id = license.get("license_id") if isinstance(license, dict) else None
            fixed = library.licenses.get(license_id) if isinstance(license_id, str) else None
            if fixed is None or license_id in supplied:
                note(INCOMPLETE_RESTRICTIONS)
                continue
            if (license.get("headline") != fixed.get("headline") or license.get("body") != fixed.get("body")
                    or license.get("url") != fixed.get("url")):
                note(INCOMPLETE_RESTRICTIONS)
            supplied.add(license_id)
    if "completed_utc" not in sample:
        note("Completion metadata was absent; the recorded date kind is unknown.")
    elif recorded_kind == "unknown":
        note("No recorded completion or input-use timestamp was available.")
    excluded = sample.get("excluded_chunks")
    if isinstance(excluded, list):
        known = [chunk for chunk in excluded
                 if isinstance(chunk, int) and not isinstance(chunk, bool) and -1 <= chunk <= 2**53 - 1]
        if known:
            note(f"Provisional input: {len(known)} truncated chunks were excluded; "
                 "measurements reflect only the reads present.")
        if len(known) != len(excluded):
            note("Input-exclusion detail was invalid; collection completeness could not be confirmed.")
    else:
        note("Input-exclusion status was unavailable; collection completeness could not be confirmed.")

    audited = _visible_algorithms(library)
    units = audited_units()
    catalog = [row for row in sample["catalog"] if isinstance(row, dict)
               and isinstance(row.get("algorithm_id"), str) and _ALGORITHM.match(row["algorithm_id"])
               and row["algorithm_id"] in audited]
    visible = {row["algorithm_id"] for row in catalog}
    selection_issues: dict[str, str] = {}
    for row in catalog:
        if row.get("status") in ("unreadable", "ambiguous", "not run"):
            selection_issues[row["algorithm_id"]] = row["status"]
    for field, status in (("unreadable_results", "unreadable"), ("ambiguous_results", "ambiguous")):
        listed = sample.get(field)
        if not isinstance(listed, list):
            note("Result-selection detail was unavailable; method omissions may be incomplete.")
            continue
        for algorithm_id in listed:
            if isinstance(algorithm_id, str) and algorithm_id in visible:
                if status == "unreadable" or selection_issues.get(algorithm_id) != "unreadable":
                    selection_issues[algorithm_id] = status
            else:
                note("An unrecognized result-selection identity was omitted from this report.")
    if sample.get("malformed_result_selections") is True:
        note("Malformed result selections were detected; affected method results may be absent.")
    elif sample.get("malformed_result_selections") is not False:
        note("Result-selection detail was unavailable; method omissions may be incomplete.")
    for algorithm_id, status in selection_issues.items():
        note(f"Method {algorithm_id}: {'not run' if status == 'not run' else f'selected result {status}'}; "
             "no scalar rows.")

    catalog_by_id = {row["algorithm_id"]: row for row in catalog}
    licenses: dict[str, dict[str, str]] = {}
    methods: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    recorded: set[str] = set()
    restrictions_noted: set[str] = set()
    underpowered: dict[str, dict[str, Any]] = {}
    scanned = 0
    for algorithm in sample["algorithms"]:
        if not isinstance(algorithm, dict):
            continue
        algorithm_id = algorithm.get("algorithm_id")
        if algorithm_id not in visible or algorithm.get("hidden") is True:
            continue
        recorded.add(algorithm_id)
        if algorithm_id in selection_issues:
            continue
        status = algorithm.get("status")
        skip_reason = algorithm.get("skip_reason") if status == "skipped" else None
        skip_reason = skip_reason if skip_reason in SKIP_REASONS else None
        if status in ("skipped", "failed"):
            note(f"Method {algorithm_id}: {status}{f' ({skip_reason})' if skip_reason else ''}; no scalar rows.")
            continue
        if status != "ok" or not isinstance(algorithm.get("metrics"), list):
            note(f"Method {algorithm_id}: no eligible scalar rows.")
            continue
        if _underpowered(algorithm.get("underpowered")) is not None:
            note(f"Method {algorithm_id}: {UNDERPOWERED_NOTE}")
            underpowered[algorithm_id] = _underpowered(algorithm["underpowered"])
        provenance = algorithm.get("provenance")
        for metric in algorithm["metrics"]:
            scanned += 1
            if scanned > 100_000:
                raise ReportRefused("the report exceeds the export processing limit", rows=len(rows))
            metric_id = metric.get("metric_id") if isinstance(metric, dict) else None
            entry = (library.entry(algorithm_id, metric_id)
                     if isinstance(metric_id, str) and _METRIC.match(metric_id) else None)
            if entry is None:
                note("Some metrics were omitted because their identities were not in the audited registry.")
                continue
            value_kind = entry.get("value_kind")
            if value_kind == "vector":
                note("Some metrics were omitted because their values were not scalar.")
                continue
            valid, value, nonfinite = _scalar(metric.get("value"), algorithm_id, metric_id, value_kind)
            if not valid:
                note("Some metrics were omitted because their values were unsupported.")
                continue
            if not _valid_provenance(provenance, sample):
                note("Some metrics were omitted because their provenance was unavailable or invalid.")
                continue
            unit = metric.get("unit")
            producer_unit = units.get((algorithm_id, metric_id))
            if (not (unit is None or unit == producer_unit)
                    or (producer_unit is not None and producer_unit not in UNITS)):
                note("Some metrics were omitted because their units did not match audited producer metadata.")
                continue
            if nonfinite:
                note("Some nonfinite numeric values are unavailable in this report.")
            support = metric.get("n")
            if support is not None and not (isinstance(support, int) and not isinstance(support, bool)
                                            and 0 <= support <= 2**53 - 1):
                note("Some recorded support counts were invalid and are shown as unavailable.")
                support = None
            state = metric.get("state")
            if state is not None and state not in STATES:
                note("Some interpretation states were unavailable.")
                state = None
            allowed = (NOT_INTERPRETABLE_REASONS if state == "not_interpretable"
                       else NO_REFERENCE_REASONS if state == "no_reference" else ())
            reason = metric.get("reason")
            if reason is not None and not (isinstance(reason, str) and reason in allowed):
                note("Some interpretation reasons were omitted because their state or reason was invalid.")
            reason = reason if isinstance(reason, str) and reason in allowed else None
            flag_ids, license_ids = _restrictions(library, algorithm_id, metric_id, algorithm.get("algorithm_version"))
            supplied_entry = metric.get("entry") if isinstance(metric.get("entry"), dict) else {}
            if (not _same_ids(supplied_entry.get("flags"), flag_ids)
                    or not _same_ids(supplied_entry.get("license_ids"), license_ids)
                    or any(license_id not in supplied for license_id in license_ids)
                    # The library loader does not enforce the flag or license vocabulary.
                    or any(flag not in RESTRICTION_FLAG_TEXT for flag in flag_ids)
                    or any(license_id not in library.licenses for license_id in license_ids)):
                note(INCOMPLETE_RESTRICTIONS)
            flags = [VIRAL_PRIVACY_TEXT if flag == "PRIVACY" and algorithm_id == "viral-reads"
                     else RESTRICTION_FLAG_TEXT[flag] for flag in flag_ids if flag in RESTRICTION_FLAG_TEXT]
            license_ids = [license_id for license_id in license_ids if license_id in library.licenses]
            for license_id in license_ids:
                if license_id not in licenses:
                    fixed = library.licenses[license_id]
                    licenses[license_id] = {"id": license_id,
                                            "headline": scrub.curated(str(fixed.get("headline") or "")),
                                            "body": scrub.curated(str(fixed.get("body") or ""))}
            key = f"{algorithm_id}/{metric_id}"
            variant = f"{key}:{','.join(license_ids)}"
            if variant not in restrictions_noted:
                restrictions_noted.add(variant)
                for flag_text in flags:
                    note(f"Metric {key}: {flag_text}")
                for license_id in license_ids:
                    note(f"License {license_id}: {licenses[license_id]['headline']} — {licenses[license_id]['body']}")
            if len(rows) >= MAX_ROWS:
                raise ReportRefused(f"the report exceeds the {MAX_ROWS:,} row export limit", rows=len(rows) + 1)
            if algorithm_id not in methods:
                # Catalogue text comes from the library's method catalogue, as the API
                # builds it; the payload's copy is only compared.
                # A difference is shown with the method, not in the limitations, so the
                # limitations stay identical to the browser export's.
                source = dict(library.method_catalog.get(algorithm_id) or {})
                served = catalog_by_id.get(algorithm_id, {})
                expected_requirements = list(source.get("requirements") or ())
                catalog_differs = any(
                    served.get(key) != (expected_requirements if key == "requirements" else source.get(key))
                    for key in ("question", "description", "limitations", "requirements"))
                methods[algorithm_id] = {
                    "question": _curated(source.get("question"), scrub),
                    "description": _curated(source.get("description"), scrub),
                    "limitations": _curated(source.get("limitations"), scrub),
                    "requirements": _curated(source.get("requirements"), scrub),
                    "licenses": [],
                    "catalog_differs": catalog_differs,
                    "provenance": {
                        "tool": provenance["tool"], "tool_version": provenance["tool_version"],
                        "parameters_sha256": provenance["parameters_sha256"],
                        "attempt_id": provenance.get("attempt_id"), "reference_id": provenance["reference_id"],
                        "computed_at": provenance["timestamp_utc"],
                    },
                }
            for license_id in license_ids:
                if license_id not in methods[algorithm_id]["licenses"]:
                    methods[algorithm_id]["licenses"].append(license_id)
            reference = entry.get("reference")
            reference_text = None
            if isinstance(reference, dict):
                parts = [part for part in (reference.get("kind"), reference.get("source")) if part]
                reference_text = _free_text(": ".join(map(str, parts)), scrub, note) if parts else None
            next_steps = entry.get("next_steps") if isinstance(entry.get("next_steps"), dict) else {}
            # Labels are curated library text: ROOT/home only, never word-redacted.
            raw_label = entry.get("label")
            label = (scrub.curated(raw_label) if isinstance(raw_label, str) and raw_label
                     else f"{metric_id} (label not recorded)")
            rows.append({
                "algorithm_id": algorithm_id, "metric_id": metric_id, "label": label,
                "raw_value": value, "raw_unit": unit, "recorded_support_n": support,
                "status": status, "skip_reason": None,
                "interpretation_state": state, "interpretation_reason": reason,
                "tool": provenance["tool"], "tool_version": provenance["tool_version"],
                "parameters_sha256": provenance["parameters_sha256"],
                "attempt_id": provenance.get("attempt_id"), "reference_id": provenance["reference_id"],
                "computed_at": provenance["timestamp_utc"],
                "flags": flags, "license_ids": license_ids, "reference": reference_text,
                "confounders": _free_text(entry.get("confounders"), scrub, note),
                "analytical_next_steps": _free_text(next_steps.get("analytical"), scrub, note),
                "clinical_next_steps": _free_text(next_steps.get("clinical_setting"), scrub, note),
            })
    for row in catalog:
        algorithm_id = row["algorithm_id"]
        if algorithm_id in recorded or algorithm_id in selection_issues:
            continue
        if row.get("status") in ("skipped", "failed"):
            reason = row.get("skip_reason") if row["status"] == "skipped" else None
            suffix = f" ({reason})" if reason in SKIP_REASONS else ""
            note(f"Method {algorithm_id}: {row['status']}{suffix}; no scalar rows.")
    if not rows:
        raise ReportRefused("no eligible scalar metrics are available for this sample", rows=0)
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at, "research_use_only": True,
        "example": sample["example"], "sample_id": id12, "source_id": sample["source_id"],
        "full_input_id": sample["full_input_id"], "recorded_utc": sample["run_utc"] or None,
        "recorded_utc_kind": recorded_kind, "interpretation_version": sample["interpretation_version"],
        "cohort_version": sample.get("cohort_version"), "limitations": note.items,
        "metrics": rows, "methods": methods, "licenses": licenses,
        "underpowered": {key: value for key, value in underpowered.items() if key in methods},
    }


def _underpowered(value: Any) -> dict[str, Any] | None:
    """A recorded ``lab run --below-depth`` mark, or None when absent or malformed."""

    if not isinstance(value, dict) or value.get("kind") not in ("eligible_reads", "counted_reads", "depth_x"):
        return None
    numbers = [value.get("floor"), value.get("observed")]
    if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in numbers):
        return None
    return {"floor": value["floor"], "observed": value["observed"], "kind": value["kind"]}


def _underpowered_text(value: dict[str, Any]) -> str:
    if value["kind"] == "depth_x":
        return f"{value['observed']:.3g}x of {value['floor']:g}x depth"
    noun = "counted reads" if value["kind"] == "counted_reads" else "eligible reads"
    return f"{int(value['observed']):,} of {int(value['floor']):,} {noun}"


# -- HTML ---------------------------------------------------------------------------------

_STYLE = """
html { color-scheme: light; }
body { margin: 0; padding: 1rem; max-width: 60rem; font: 15px/1.45 system-ui, sans-serif; color: #111; background: #fff; }
h1 { font-size: 1.5rem; margin: 0.5rem 0; }
h2 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; border-bottom: 1px solid #ccc; }
h3 { font-size: 1.05rem; margin: 1rem 0 0.25rem; }
.marks { font-weight: 700; letter-spacing: 0.02em; border: 2px solid #111; padding: 0.5rem; }
.flag { display: inline-block; font-weight: 700; background: #ffe7a3; color: #111; padding: 0.15rem 0.5rem; border-radius: 1rem; margin: 0.15rem 0; }
.callout { border: 2px solid #b06000; background: #fff6e0; padding: 0.5rem 0.75rem; margin: 0.75rem 0; }
.clia p { margin: 0.4rem 0; }
.flag-pill { font-weight: 700; background: #ffe7a3; padding: 0 0.3rem; border-radius: 0.5rem; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
th, td { border: 1px solid #bbb; padding: 0.3rem 0.45rem; text-align: left; vertical-align: top; font-size: 0.92rem; }
th { background: #f0f0f0; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.2rem 0.75rem; margin: 0.5rem 0; }
dt { font-weight: 700; }
dd { margin: 0; overflow-wrap: anywhere; }
.license { border-left: 4px solid #777; padding: 0.25rem 0.75rem; margin: 0.5rem 0; background: #f7f7f7; }
.provenance { font-size: 0.88rem; color: #333; overflow-wrap: anywhere; }
.detail { font-size: 0.9rem; margin: 0.2rem 0; }
footer { margin-top: 2rem; border-top: 2px solid #111; padding-top: 0.5rem; font-weight: 700; }
ul { padding-left: 1.25rem; }
@media print { body { padding: 0; } .callout, .marks { break-inside: avoid; } }
"""



def _e(value: Any) -> str:
    """HTML-escape one value; text was scrubbed when the report was projected."""

    return html.escape("unavailable" if value is None or value == "" else str(value), quote=True)


def _inline(markdown: str) -> str:
    """The page's inline markdown subset: bold, ``[[flag]]`` pills; links keep only their text."""

    out: list[str] = []
    last = 0
    for match in _INLINE.finditer(markdown):
        out.append(html.escape(markdown[last:match.start()]))
        if match.group(1):
            out.append(f"<strong>{html.escape(match.group(1))}</strong>")
        elif match.group(2):
            out.append(f'<span class="flag-pill">{html.escape(match.group(2))}</span>')
        else:
            out.append(html.escape(match.group(3)))
        last = match.end()
    out.append(html.escape(markdown[last:]))
    return "".join(out)


def _paragraphs(markdown: str) -> str:
    return "".join(f"<p>{_inline(block.strip())}</p>\n"
                   for block in re.split(r"\n\s*\n", markdown) if block.strip())


def _raw_value(row: dict[str, Any]) -> str:
    value = row["raw_value"]
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_html(report: dict[str, Any], clia_short: str) -> str:
    """Self-contained HTML: inline CSS, no scripts, no event handlers, no external resources.

    Every string in ``report`` and ``clia_short`` is already scrubbed (see
    ``project_report`` and ``build_html``); this only escapes.
    """

    e = _e

    def field(label: str, value: Any) -> str:
        return f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>\n"

    out: list[str] = []
    push = out.append
    title = f"Traceback Lab research report {report['sample_id']}"
    push("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
    push('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    push('<meta name="referrer" content="no-referrer">\n')
    push(f"<title>{e(title)}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n")
    marks = RESEARCH_MARKS + (" · SYNTHETIC EXAMPLE" if report["example"] else "")
    push(f'<p class="marks" id="marks">{e(marks)}</p>\n')
    push(f"<h1>{e(title)}</h1>\n")
    push('<div class="callout" id="clia-flag"><p class="flag">FLAG: CLIA</p>\n')
    push(f"<p>{e(CLINICAL_ADVICE_WARNING)}</p>\n")
    push("<p>This file may reach a donor or clinician. It is a research record of recorded values, "
         "not a result for diagnosis, prevention, treatment or health assessment.</p></div>\n")
    push('<section class="clia" id="clia"><h2>What CLIA means for this demo</h2>\n')
    push(_paragraphs(clia_short))
    push("</section>\n")
    push('<section id="identity"><h2>Captured report</h2>\n<dl>\n')
    push(field("Schema version", report["schema_version"]))
    push(field("Captured UTC", report["generated_at"]))
    push(field("Sample", report["sample_id"]))
    push(field("Input", report["full_input_id"]))
    push(field("Source", report["source_id"]))
    push(field("Recorded date (UTC)", report["recorded_utc"]))
    push(field("Recorded date kind", report["recorded_utc_kind"]))
    push(field("Interpretation version", report["interpretation_version"]))
    push(field("Cohort version", report["cohort_version"]))
    push(field("Included scalar rows", len(report["metrics"])))
    push("</dl>\n<p>Values are the exact recorded values with their declared units; the Lab page's "
         "display rounding is not applied in this file.</p>\n</section>\n")
    if report.get("underpowered"):
        push('<div class="callout underpowered" id="underpowered"><p class="flag">'
             f"{e(UNDERPOWERED_BANNER)}</p>\n<ul>\n")
        for algorithm_id, value in report["underpowered"].items():
            push(f"<li>{e(algorithm_id)}: {e(_underpowered_text(value))}</li>\n")
        push("</ul></div>\n")
    push('<section id="limitations"><h2>Limitations</h2>\n<ul>\n')
    for item in report["limitations"]:
        push(f"<li>{e(item)}</li>\n")
    push("</ul>\n</section>\n")
    push('<section id="metrics"><h2>Scalar metrics</h2>\n')
    push('<table class="metrics" id="metrics-table"><thead><tr><th>Method</th><th>Metric</th><th>Value</th>'
         "<th>Unit</th><th>Interpretation</th><th>Reason</th><th>Support count</th><th>Flags</th></tr></thead>\n<tbody>\n")
    for row in report["metrics"]:
        key = f"{row['algorithm_id']}/{row['metric_id']}"
        flags = "; ".join(row["flags"]) or "none"
        push(f'<tr data-metric="{e(key)}"><td>{e(row["algorithm_id"])}</td>'
             f"<td>{e(row['label'])}<br><small>{e(row['metric_id'])}</small></td>"
             f'<td class="num">{e(_raw_value(row))}</td>'
             f"<td>{e(row['raw_unit'] or 'unit not declared')}</td>"
             f"<td>{e(row['interpretation_state'])}</td><td>{e(row['interpretation_reason'])}</td>"
             f'<td class="num">{e(row["recorded_support_n"])}</td><td>{e(flags)}</td></tr>\n')
    push("</tbody></table>\n</section>\n")
    for algorithm_id, method in report["methods"].items():
        push(f'<section class="method" data-method="{e(algorithm_id)}"><h2>{e(algorithm_id)}</h2>\n')
        if algorithm_id in report.get("underpowered", {}):
            push(f'<p class="flag underpowered">{e(UNDERPOWERED_BANNER)}</p>\n')
        push("<dl>\n")
        push(field("Question", method["question"]))
        push(field("Method", method["description"]))
        push(field("Requirements", method["requirements"]))
        push(field("Limitations", method["limitations"]))
        push("</dl>\n")
        if method["catalog_differs"]:
            push('<p class="detail catalog-differs">The served method text differed from the curated '
                 "method catalogue; the curated text is shown.</p>\n")
        for license_id in method["licenses"]:
            license = report["licenses"][license_id]
            push(f'<div class="license"><p><strong>License restriction ({e(license_id)}): '
                 f"{e(license['headline'])}</strong></p><p>{e(license['body'])}</p></div>\n")
        for row in report["metrics"]:
            if row["algorithm_id"] != algorithm_id:
                continue
            push(f'<article class="metric" data-metric="{e(row["algorithm_id"])}/{e(row["metric_id"])}">')
            push(f"<h3>{e(row['label'])}</h3>\n<dl>\n")
            push(field("Metric ID", row["metric_id"]))
            unit = f" {row['raw_unit']}" if row["raw_unit"] else " (unit not declared)"
            push(field("Exact recorded value", "unavailable" if row["raw_value"] is None else f"{_raw_value(row)}{unit}"))
            push(field("Interpretation", row["interpretation_state"]))
            push(field("Interpretation reason", row["interpretation_reason"]))
            push(field("Recorded support count", row["recorded_support_n"]))
            push(field("Reference", row["reference"]))
            push(field("Confounders", row["confounders"]))
            push(field("Next steps (analytical)", row["analytical_next_steps"]))
            push("</dl>\n")
            for flag in row["flags"]:
                push(f'<p class="flag">{e(flag)}</p>\n')
            push(f'<p class="flag advice">{e(CLINICAL_ADVICE_WARNING)}</p>\n')
            if row["clinical_next_steps"]:
                push(f'<p class="detail"><strong>Clinical setting:</strong> {e(row["clinical_next_steps"])}</p>\n')
                push(f'<p class="flag advice">{e(CLINICAL_ADVICE_WARNING)}</p>\n')
            push("</article>\n")
        provenance = method["provenance"]
        push('<p class="provenance"><strong>Result provenance — </strong>'
             f"Tool: {e(provenance['tool'])}; Tool version: {e(provenance['tool_version'])}; "
             f"Computed UTC: {e(provenance['computed_at'])}. "
             f"Parameters SHA-256: {e(provenance['parameters_sha256'])}; "
             f"Attempt ID: {e(provenance['attempt_id'])}; "
             f"Reference asset ID: {e(provenance['reference_id'])}.</p>\n</section>\n")
    push(f'<footer id="frozen"><p>{e(FROZEN_FOOTER)}</p>\n')
    push(f"<p>Captured {e(report['generated_at'])} for input {e(report['full_input_id'])}. {e(marks)}</p>\n")
    push("</footer>\n</body>\n</html>\n")
    return "".join(out)


def build_html(sample: dict[str, Any], generated_at: str, scrub: ReportScrubber,
               library: Library | None = None) -> tuple[dict[str, Any], bytes]:
    """Project, render and size-check one sample; never a partial result.

    Both limits the browser applies hold here: the projection's compact JSON
    (the measure ``lab-report.js`` uses, without its display-value columns)
    and the HTML file itself must each fit in 5 MiB.
    """

    from .web.server import clia_copy

    report = project_report(sample, generated_at, scrub, library)
    projection = len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if projection > MAX_BYTES:
        raise ReportRefused(f"the report exceeds the {MAX_BYTES // (1024 * 1024)} MiB export limit",
                            bytes=projection, rows=len(report["metrics"]))
    try:
        clia_short = clia_copy()["short"]
        if not isinstance(clia_short, str) or not clia_short.strip():
            raise ValueError("empty")
    except (OSError, IndexError, KeyError, ValueError, TypeError, AttributeError) as error:
        raise ReportRefused(f"the CLIA explainer copy could not be read ({type(error).__name__})") from None
    document = render_html(report, scrub.curated(clia_short)).encode("utf-8")
    if len(document) > MAX_BYTES:
        raise ReportRefused(f"the report exceeds the {MAX_BYTES // (1024 * 1024)} MiB export limit",
                            bytes=len(document), rows=len(report["metrics"]))
    return report, document


def report_filename(sample_id: str, generated_at: str) -> str:
    stamp = generated_at[:19].replace("-", "").replace(":", "")
    return f"traceback-research-{sample_id}-{stamp}Z.html"


def add_report_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    parser = commands.add_parser(
        "report", help="write one sample's research report as a self-contained HTML file",
        description=("Write the captured research report of one sample as a single HTML file that "
                     "renders without a server or JavaScript. Research use only."),
    )
    parser.add_argument("--html", required=True, metavar="ID12", dest="html_id12",
                        help="the sample (id12 or a longer source-id prefix) to report")
    parser.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help="file to write (default: traceback-research-<id12>-<utc>Z.html in the current directory)")
    common(parser)
    parser.set_defaults(handler=run_report)


class OutputExists(Exception):
    """``--out`` already exists (or appeared while the report was written)."""


def _write_new(out: Path, document: bytes) -> None:
    """Publish ``document`` at ``out`` complete or not at all, never replacing an existing file.

    The bytes are written and fsynced to a sibling staging file with a short
    random name (well under NAME_MAX whatever ``out`` is called), then
    hard-linked into place: ``os.link`` fails if ``out`` exists, even when it
    appeared after the check, and only that failure is ``OutputExists``.  A
    filesystem without hard links gets an exclusive create of ``out`` itself,
    removed again if the write fails.  The staging file is removed only when
    this call created it.
    """

    staging = out.with_name(f".traceback-report-{os.getpid()}-{secrets.token_hex(8)}.part")
    created_staging = False
    try:
        with open(staging, "xb") as handle:
            created_staging = True
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, out)
        except FileExistsError:
            raise OutputExists from None
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV):
                raise
            created = False
            try:
                with open(out, "xb") as handle:
                    created = True
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                raise OutputExists from None
            except BaseException:
                if created:
                    try:
                        out.unlink()
                    except OSError:
                        pass
                raise
    finally:
        if created_staging:
            try:
                staging.unlink()
            except OSError:
                pass


def run_report(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    from .web.api import LabApi

    store.check_version()
    requested = args.html_id12
    if not isinstance(requested, str) or not _ID12.match(requested):
        raise LabProblem(
            "TBX-LAB-017",
            cause="the sample id must be 12 to 64 lowercase hexadecimal characters",
            fix="`traceback lab runs` lists samples by id12; pass one of them to --html",
            data={"requested": requested[:64] if isinstance(requested, str) else None},
        )
    api = LabApi(store)
    sample = api.sample(requested)
    if sample.get("found") is not True:
        candidates = [c for c in sample.get("candidates", []) if isinstance(c, str)][:10]
        raise LabProblem(
            "TBX-LAB-017",
            cause=(f"{requested!r} matches {len(candidates)} samples; pass a longer prefix" if candidates
                   else f"no sample {requested!r} under this ROOT"),
            fix="`traceback lab runs` lists samples; pass the same --root you ran with",
            data={"requested": requested, "candidates": candidates},
        )
    generated_at = _utcnow()
    scrubber = report_scrubber(store.root)
    try:
        report, document = build_html(sample, generated_at, scrubber, api.libraries.get())
    except ReportRefused as refused:
        raise LabProblem(
            "TBX-LAB-034",
            cause=refused.cause,
            fix=("select a sample whose report fits the 5,000-row and 5 MiB limits, or open the Lab page "
                 "and use Export / print for the live view"),
            data={"sample_id": sample["id12"], **refused.data},
        ) from None
    out = args.out if args.out is not None else Path.cwd() / report_filename(report["sample_id"], generated_at)
    out = out.expanduser().absolute()
    exists = LabProblem(
        "TBX-LAB-034",
        cause="the output path already exists; nothing was overwritten",
        fix="pass a new --out PATH, or move the existing file aside",
        data={"sample_id": report["sample_id"], "exists": True},
    )
    try:
        present = out.exists() or out.is_symlink()
    except OSError as error:
        raise LabProblem(
            "TBX-LAB-034",
            cause=f"the output path could not be checked ({type(error).__name__}: {error.strerror or 'error'})",
            fix="pass an --out PATH inside a readable, writable existing directory",
            data={"sample_id": report["sample_id"], "errno": error.errno},
        ) from None
    if present:
        raise exists
    try:
        _write_new(out, document)
    except OutputExists:
        raise exists from None
    except OSError as error:
        raise LabProblem(
            "TBX-LAB-034",
            cause=f"the output file could not be written ({type(error).__name__}: {error.strerror or 'error'})",
            fix="pass an --out PATH inside a writable existing directory",
            data={"sample_id": report["sample_id"], "errno": error.errno},
        ) from None
    digest = hashlib.sha256(document).hexdigest()
    data = {
        "root": str(store.root), "sample_id": report["sample_id"], "source_id": report["source_id"],
        "full_input_id": report["full_input_id"], "generated_at": generated_at,
        "path": str(out), "size": len(document), "sha256": digest, "rows": len(report["metrics"]),
        "example": report["example"], "research_use_only": True,
    }
    payload = _local_envelope(_result("lab report", "ok", f"wrote research report for {report['sample_id']}", data=data))
    lines = [
        f"ROOT  {store.root}",
        f"WROTE     {out}",
        f"SAMPLE    {report['sample_id']}" + ("  (synthetic example)" if report["example"] else ""),
        f"CAPTURED  {generated_at}",
        f"ROWS      {len(report['metrics'])}",
        f"SIZE      {len(document)} bytes",
        f"SHA256    {digest}",
        "Research use only. Not CLIA-validated. Not a diagnosis. Values are frozen at export time.",
        "FLAG: CLIA — this file can reach a donor or clinician; review its intended use before sharing.",
    ]
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


__all__ = [
    "CLINICAL_ADVICE_WARNING",
    "FROZEN_FOOTER",
    "MAX_BYTES",
    "MAX_ROWS",
    "RESEARCH_MARKS",
    "RESTRICTION_FLAG_TEXT",
    "ReportScrubber",
    "UNITS",
    "WITHHELD_TEXT",
    "ReportRefused",
    "add_report_parser",
    "build_html",
    "project_report",
    "render_html",
    "report_filename",
    "report_scrubber",
    "run_report",
]
