"""``traceback lab report --html ID12 [--out PATH]``: one self-contained HTML research report.

The file carries the same captured research report the Lab page's
**Export / print** panel produces, rendered server-side from the sample
projection ``traceback_lab.web.api.LabApi.sample`` serves (the read-time
interpretation engine runs inside that projection).  A scientist can open
or share the file without a running server: it has no scripts, no inline
event handlers and no external resources, so it renders with JavaScript
disabled.

What the file shows is what the page would show: visible registered scalar
metrics with their raw recorded value and declared unit, interpretation
state and fixed reason, recorded support count, audited flags and curated
license restrictions, per-method provenance, and the report's limitations.
No display formatting is re-implemented here: the Lab's number formatting
lives in ``lab-format.js`` and has no Python equivalent, so the file prints
the exact recorded values (the "Exact API value" of the print view).

Nothing that the page would not show enters the file.  Every string goes
through ``Scrubber`` (ROOT, home, login name, host name) and free-text
metadata is dropped when it looks like a path, a credential, a long
hexadecimal identifier or a read sequence; then everything is HTML-escaped.
The same 5,000-row and 5 MiB limits as the browser export apply; an
over-limit report is refused (TBX-LAB-034) without writing a partial file.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import math
import os
import re
import socket
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .interpretation.engine import NO_REFERENCE_REASONS, NOT_INTERPRETABLE_REASONS
from .problems import EXIT_OK, LabProblem
from .scrub import Scrubber
from .store import LabStore

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
SCHEMA_VERSION = "2"

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
RESEARCH_MARKS = "RESEARCH USE ONLY · NOT CLIA VALIDATED · NOT A DIAGNOSIS"
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
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID12 = re.compile(r"^[0-9a-f]{12,64}$")
_ALGORITHM = re.compile(r"^[a-z0-9-]{1,40}$")
_METRIC = re.compile(r"^[a-z0-9_]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z$")
_TOOL = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_TOOL_VERSION = re.compile(r"^[A-Za-z0-9_.+:-]{1,40}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ATTEMPT = re.compile(r"^a-\d{8}T\d{12}-[0-9a-f]{8}$")
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\\]")
_SECRET = re.compile(r"(?:password|secret|token|api[_ -]?key|bearer|AKIA|sk-[A-Za-z0-9]{8}|[ACGTN]{30})", re.I)
_PATH_LIKE = re.compile(r"(?:^|[\s(:])/[A-Za-z][\w.-]*(?:/[\w.-]+)+")
_LONG_HEX = re.compile(r"\b[0-9a-f]{32,}\b", re.I)
_INLINE = re.compile(r"\*\*(.+?)\*\*|\[\[(.+?)\]\]|\[([^\]]+)\]\((https?:[^)]+)\)")


def _utcnow() -> str:
    """The capture instant (seconds precision, like the browser's ISO stamp)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hostname() -> str | None:
    try:
        name = socket.gethostname().split(".")[0]
    except OSError:
        return None
    return name if len(name) >= 6 and name.lower() != "localhost" else None


def report_scrubber(root: Path) -> Scrubber:
    """ROOT, home, login and the host name never reach the file."""

    host = _hostname()
    return Scrubber(root=root, chunk_stems=(host,) if host else (), max_line=4000)


class ReportRefused(Exception):
    """A report that cannot be built; ``cause`` becomes the TBX-LAB-034 CAUSE line."""

    def __init__(self, cause: str, **data: Any) -> None:
        super().__init__(cause)
        self.cause = cause
        self.data = data


def _instant(value: Any, pattern: re.Pattern[str] = _INSTANT) -> bool:
    if not isinstance(value, str) or not pattern.match(value):
        return False
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S" if len(value) >= 19 else "%Y-%m-%dT%H:%M")
    except ValueError:
        return False
    return True


def _safe_metadata(value: Any, scrub: Callable[[str], str]) -> str | None:
    """Free text the page would show, or nothing: never a path, secret or long id."""

    if not isinstance(value, str) or not value or len(value) > 1200:
        return None
    if _UNSAFE_CONTROL.search(value) or _SECRET.search(value) or _PATH_LIKE.search(value) or _LONG_HEX.search(value):
        return None
    scrubbed = scrub(value)
    return scrubbed or None


def _metadata_list(value: Any, scrub: Callable[[str], str]) -> str | None:
    if isinstance(value, str):
        return _safe_metadata(value, scrub)
    if not isinstance(value, list):
        return None
    items = [item for item in (_safe_metadata(entry, scrub) for entry in value[:12]) if item]
    return "; ".join(items) or None


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


# The categorical values lab-report.js accepts; any other category is omitted.
_CATEGORIES = {
    ("sex-check", "call"): ("male", "female", "indeterminate"),
    ("copy-number", "pon_policy"): ("none", "lab_loo_v1"),
}
_BOOLEAN_CATEGORIES = frozenset({("copy-number", "identifiable"), ("meth-tf", "identifiable")})


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


def project_report(sample: dict[str, Any], generated_at: str, scrubber: Scrubber) -> dict[str, Any]:
    """The captured research report (ResearchReportV2 identity, limitations, rows, context).

    Mirrors ``lab-report.js``'s projection over the same API payload, using
    the library-backed ``entry`` the API attaches to each metric as the source
    of value kind, flags and license ids.  Raises ``ReportRefused`` when the
    sample cannot be reported or the row limit is exceeded.
    """

    scrub = scrubber.line
    if not isinstance(sample, dict) or sample.get("found") is not True:
        raise ReportRefused("the sample is not in the Lab store")
    if sample.get("state"):
        raise ReportRefused(f"the sample's recorded result is unavailable ({sample['state']})",
                            state=str(sample["state"]))
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

    limitations: list[str] = list(FIXED_LIMITATIONS)

    def note(message: str) -> None:
        if message not in limitations:
            limitations.append(message)

    licenses: dict[str, dict[str, str]] = {}
    supplied = sample.get("licenses")
    if not isinstance(supplied, list):
        note(INCOMPLETE_RESTRICTIONS)
    else:
        for license in supplied:
            license_id = license.get("license_id") if isinstance(license, dict) else None
            headline = _safe_metadata(license.get("headline"), scrub) if isinstance(license, dict) else None
            body = _safe_metadata(license.get("body"), scrub) if isinstance(license, dict) else None
            if (not isinstance(license_id, str) or not _ALGORITHM.match(license_id)
                    or license_id in licenses or not headline or not body):
                note(INCOMPLETE_RESTRICTIONS)
                continue
            licenses[license_id] = {"id": license_id, "headline": headline, "body": body}
    if "completed_utc" not in sample:
        note("Completion metadata was absent; the recorded date kind is unknown.")
    elif recorded_kind == "unknown":
        note("No recorded completion or input-use timestamp was available.")
    excluded = sample.get("excluded_chunks")
    if isinstance(excluded, list):
        known = [chunk for chunk in excluded if isinstance(chunk, int) and not isinstance(chunk, bool) and chunk >= -1]
        if known:
            note(f"Provisional input: {len(known)} truncated chunks were excluded; "
                 "measurements reflect only the reads present.")
        if len(known) != len(excluded):
            note("Input-exclusion detail was invalid; collection completeness could not be confirmed.")
    else:
        note("Input-exclusion status was unavailable; collection completeness could not be confirmed.")

    catalog = [row for row in sample["catalog"] if isinstance(row, dict)
               and isinstance(row.get("algorithm_id"), str) and _ALGORITHM.match(row["algorithm_id"])]
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
    methods: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    recorded: set[str] = set()
    restrictions_noted: set[str] = set()
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
        provenance = algorithm.get("provenance")
        for metric in algorithm["metrics"]:
            if not isinstance(metric, dict) or not isinstance(metric.get("metric_id"), str) \
                    or not _METRIC.match(metric["metric_id"]):
                note("Some metrics were omitted because their identities were invalid.")
                continue
            entry = metric.get("entry")
            if not isinstance(entry, dict):
                note("Some metrics were omitted because they have no interpretation library entry.")
                continue
            value_kind = entry.get("value_kind")
            if value_kind == "vector":
                note("Some metrics were omitted because their values were not scalar.")
                continue
            valid, value, nonfinite = _scalar(metric.get("value"), algorithm_id, metric["metric_id"], value_kind)
            if not valid:
                note("Some metrics were omitted because their values were unsupported.")
                continue
            if not _valid_provenance(provenance, sample):
                note("Some metrics were omitted because their provenance was unavailable or invalid.")
                continue
            unit = metric.get("unit")
            if unit is not None and (not isinstance(unit, str) or not _safe_metadata(unit, scrub) or len(unit) > 40):
                note("Some metrics were omitted because their units were invalid.")
                continue
            if nonfinite:
                note("Some nonfinite numeric values are unavailable in this report.")
            support = metric.get("n")
            if support is not None and not (isinstance(support, int) and not isinstance(support, bool) and support >= 0):
                note("Some recorded support counts were invalid and are shown as unavailable.")
                support = None
            state = metric.get("state")
            if state is not None and state not in STATES:
                note("Some interpretation states were unavailable.")
                state = None
            allowed = (NOT_INTERPRETABLE_REASONS if state == "not_interpretable"
                       else NO_REFERENCE_REASONS if state == "no_reference" else ())
            reason = metric.get("reason")
            if reason is not None and reason not in allowed:
                note("Some interpretation reasons were omitted because their state or reason was invalid.")
                reason = None
            label = _safe_metadata(metric.get("label"), scrub) or metric["metric_id"]
            flag_ids = entry.get("flags")
            license_ids = entry.get("license_ids")
            if (not isinstance(flag_ids, list) or any(flag not in RESTRICTION_FLAG_TEXT for flag in flag_ids)
                    or not isinstance(license_ids, list) or any(item not in licenses for item in license_ids)):
                note(INCOMPLETE_RESTRICTIONS)
            flag_ids = [flag for flag in (flag_ids if isinstance(flag_ids, list) else []) if flag in RESTRICTION_FLAG_TEXT]
            license_ids = [item for item in (license_ids if isinstance(license_ids, list) else []) if item in licenses]
            flags = [VIRAL_PRIVACY_TEXT if flag == "PRIVACY" and algorithm_id == "viral-reads"
                     else RESTRICTION_FLAG_TEXT[flag] for flag in flag_ids]
            key = f"{algorithm_id}/{metric['metric_id']}"
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
                source = catalog_by_id.get(algorithm_id, {})
                methods[algorithm_id] = {
                    "question": _safe_metadata(source.get("question"), scrub),
                    "description": _safe_metadata(source.get("description"), scrub),
                    "limitations": _metadata_list(source.get("limitations"), scrub),
                    "requirements": _metadata_list(source.get("requirements"), scrub),
                    "licenses": [],
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
                parts = [_safe_metadata(reference.get("kind"), scrub), _safe_metadata(reference.get("source"), scrub)]
                reference_text = ": ".join(part for part in parts if part) or None
            next_steps = entry.get("next_steps") if isinstance(entry.get("next_steps"), dict) else {}
            rows.append({
                "algorithm_id": algorithm_id, "metric_id": metric["metric_id"], "label": label,
                "raw_value": value, "raw_unit": unit, "recorded_support_n": support,
                "interpretation_state": state, "interpretation_reason": reason,
                "flags": flags, "license_ids": license_ids, "reference": reference_text,
                "confounders": _metadata_list(entry.get("confounders"), scrub),
                "analytical_next_steps": _metadata_list(next_steps.get("analytical"), scrub),
                "clinical_next_steps": _metadata_list(next_steps.get("clinical_setting"), scrub),
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
        "cohort_version": sample.get("cohort_version"), "limitations": limitations,
        "metrics": rows, "methods": methods, "licenses": licenses,
    }


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


Scrub = Callable[[str], str]


def _e(value: Any, scrub: Scrub) -> str:
    """Scrub (ROOT, home, login, host) then HTML-escape one value; every string in the file passes here."""

    return html.escape(scrub("unavailable" if value is None or value == "" else str(value)), quote=True)


def _inline(markdown: str, scrub: Scrub) -> str:
    """The page's inline markdown subset: bold, ``[[flag]]`` pills; links keep only their text."""

    out: list[str] = []
    last = 0
    for match in _INLINE.finditer(markdown):
        if match.start() > last:
            out.append(_e(markdown[last:match.start()], scrub))
        if match.group(1):
            out.append(f"<strong>{_e(match.group(1), scrub)}</strong>")
        elif match.group(2):
            out.append(f'<span class="flag-pill">{_e(match.group(2), scrub)}</span>')
        else:
            out.append(_e(match.group(3), scrub))
        last = match.end()
    if last < len(markdown):
        out.append(_e(markdown[last:], scrub))
    return "".join(out)


def _paragraphs(markdown: str, scrub: Scrub) -> str:
    return "".join(f"<p>{_inline(block.strip(), scrub)}</p>\n"
                   for block in re.split(r"\n\s*\n", markdown) if block.strip())


def _field(label: str, value: Any, scrub: Scrub) -> str:
    return f"<dt>{_e(label, scrub)}</dt><dd>{_e(value, scrub)}</dd>\n"


def _raw_value(row: dict[str, Any]) -> str:
    value = row["raw_value"]
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_html(report: dict[str, Any], clia_short: str, scrub: Scrub | None = None) -> str:
    """Self-contained HTML: inline CSS, no scripts, no event handlers, no external resources.

    ``scrub`` (``Scrubber.line``) is applied to every string before escaping.
    """

    scrub = scrub or (lambda text: text)

    def e(value: Any) -> str:
        return _e(value, scrub)

    def field(label: str, value: Any) -> str:
        return _field(label, value, scrub)

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
    push(_paragraphs(clia_short, scrub))
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
        push(f'<section class="method" data-method="{e(algorithm_id)}"><h2>{e(algorithm_id)}</h2>\n<dl>\n')
        push(field("Question", method["question"]))
        push(field("Method", method["description"]))
        push(field("Requirements", method["requirements"]))
        push(field("Limitations", method["limitations"]))
        push("</dl>\n")
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


def build_html(sample: dict[str, Any], generated_at: str, scrubber: Scrubber) -> tuple[dict[str, Any], bytes]:
    """Project, render and size-check one sample; never a partial result."""

    from .web.server import clia_copy

    report = project_report(sample, generated_at, scrubber)
    document = render_html(report, clia_copy()["short"], scrubber.line).encode("utf-8")
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
    sample = LabApi(store).sample(requested)
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
        report, document = build_html(sample, generated_at, scrubber)
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
    if out.exists() or out.is_symlink():
        raise LabProblem(
            "TBX-LAB-034",
            cause="the output path already exists; nothing was overwritten",
            fix="pass a new --out PATH, or move the existing file aside",
            data={"sample_id": report["sample_id"], "exists": True},
        )
    staging = out.with_name(f"{out.name}.part-{os.getpid()}")
    try:
        with open(staging, "xb") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, out)
    except OSError as error:
        try:
            staging.unlink()
        except OSError:
            pass
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
    "ReportRefused",
    "add_report_parser",
    "build_html",
    "project_report",
    "render_html",
    "report_filename",
    "report_scrubber",
    "run_report",
]
