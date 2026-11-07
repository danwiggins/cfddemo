"""``traceback lab report --html``: one self-contained, scrubbed, frozen research report file."""

from __future__ import annotations

import dataclasses
import errno
import getpass
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.lab.test_lab_report_ui import _reportable_sample
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome
from traceback_lab import report_command
from traceback_lab.report_command import (
    CLINICAL_ADVICE_WARNING,
    FROZEN_FOOTER,
    RESEARCH_MARKS,
    RESTRICTION_FLAG_TEXT,
    WITHHELD_TEXT,
    ReportRefused,
    build_html,
    project_report,
    report_scrubber,
)
from traceback_lab.interpretation.engine import Entry, Library, load_library
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi, LibraryHolder
from traceback_lab.web.server import clia_copy
from traceback_runner import cli as signed_cli

REPORT_JS = Path(__file__).resolve().parents[2] / "traceback_lab/web/static/lab-report.js"
FROZEN_AT = "2026-11-07T12:00:00Z"


@pytest.fixture(scope="module")
def ran_root(tmp_path_factory, synthetic_world) -> Path:
    from traceback_runner.references import register_reference

    root = tmp_path_factory.mktemp("report-html") / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    assert signed_cli.main(["lab", "run", str(synthetic_world["chunks"]), "--root", str(root), "--json"]) == 0
    return root


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(report_command, "_utcnow", lambda: FROZEN_AT)


def _id12(root: Path) -> str:
    return LabApi(LabStore(root)).samples()["default_sample_id12"]


def _report(capsys, *argv: str) -> tuple[int, dict]:
    code = signed_cli.main(["lab", "report", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _api_scalars(sample: dict) -> dict[str, object]:
    """Every visible finite scalar the sample API serves with a library entry."""

    out = {}
    for algorithm in sample["algorithms"]:
        if algorithm.get("hidden") or algorithm.get("status") != "ok":
            continue
        for metric in algorithm.get("metrics") or []:
            entry = metric.get("entry")
            value = metric.get("value")
            if entry is None or entry.get("value_kind") in ("vector", "categorical"):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[f"{algorithm['algorithm_id']}/{metric['metric_id']}"] = value
    return out


def test_cli_writes_a_deterministic_self_contained_report(ran_root, tmp_path, capsys, frozen) -> None:
    id12 = _id12(ran_root)
    first, second = tmp_path / "a.html", tmp_path / "b.html"
    code, payload = _report(capsys, "--html", id12, "--root", str(ran_root), "--out", str(first))
    assert code == 0, payload
    assert _report(capsys, "--html", id12, "--root", str(ran_root), "--out", str(second))[0] == 0
    document = first.read_bytes()
    assert document == second.read_bytes()
    data = payload["data"]
    assert data["path"] == str(first)
    assert data["size"] == len(document)
    assert data["sha256"] == hashlib.sha256(document).hexdigest()
    assert data["generated_at"] == FROZEN_AT and data["sample_id"] == id12
    assert not list(tmp_path.glob("*part-*"))

    text = document.decode("utf-8")
    sample = LabApi(LabStore(ran_root)).sample(id12)
    assert text.startswith("<!DOCTYPE html>")
    assert RESEARCH_MARKS in text
    assert 'id="clia-flag"' in text and '<p class="flag">FLAG: CLIA</p>' in text
    assert CLINICAL_ADVICE_WARNING in text
    assert "What CLIA means for this demo" in text
    assert "42 CFR 493.3(b)(2)" in text and "Research boundary." in text
    assert FROZEN_FOOTER in text
    assert f"Captured {FROZEN_AT} for input {sample['full_input_id']}" in text
    assert sample["full_input_id"] in text and sample["source_id"] in text

    scalars = _api_scalars(sample)
    assert scalars
    for key, value in scalars.items():
        row = re.search(rf'<tr data-metric="{re.escape(key)}">(.*?)</tr>', text)
        assert row, key
        assert f'<td class="num">{value}</td>' in row.group(1), key
    # Units and support counts the API carries are in the row.
    for algorithm in sample["algorithms"]:
        for metric in algorithm.get("metrics") or []:
            key = f"{algorithm['algorithm_id']}/{metric['metric_id']}"
            if key in scalars and metric.get("unit"):
                assert f"<td>{metric['unit']}</td>" in text.split(f'data-metric="{key}">', 1)[1].split("</tr>", 1)[0]


def test_file_carries_no_host_identity_and_no_active_content(ran_root, tmp_path, capsys, frozen) -> None:
    out = tmp_path / "share.html"
    assert _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))[0] == 0
    text = out.read_text(encoding="utf-8")
    for private in (str(ran_root), str(tmp_path), str(Path.home()), "/Users/", "/private/", "/tmp/"):
        assert private not in text, private
    login = getpass.getuser()
    assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(login)}(?![A-Za-z0-9])", text)
    host = socket.gethostname().split(".")[0]
    if len(host) >= 6:
        assert host not in text
    lowered = text.lower()
    for active in ("<script", "<iframe", "<object", "<embed", "<link", "<img", "<form", "javascript:",
                   "http://", "https://", "url(", "@import", "src=", "href="):
        assert active not in lowered, active
    assert not re.search(r"\son[a-z]+\s*=", lowered)
    assert lowered.count("<style>") == 1


def test_human_output_existing_file_and_unknown_sample(ran_root, tmp_path, capsys, frozen) -> None:
    out = tmp_path / "r.html"
    code = signed_cli.main(["lab", "report", "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out)])
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[0] == f"ROOT  {ran_root}"
    assert any(line.startswith("SHA256    ") for line in lines)
    assert any("FLAG: CLIA" in line for line in lines)
    before = out.read_bytes()
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-034"
    assert out.read_bytes() == before
    code, payload = _report(capsys, "--html", "0" * 12, "--root", str(ran_root), "--out", str(tmp_path / "x.html"))
    assert code == 4 and payload["data"]["code"] == "TBX-LAB-017"
    code, payload = _report(capsys, "--html", "../x", "--root", str(ran_root), "--out", str(tmp_path / "x.html"))
    assert code == 4 and payload["data"]["code"] == "TBX-LAB-017"
    assert not (tmp_path / "x.html").exists()


def _expanded(rows: int) -> dict:
    sample = _reportable_sample()
    algorithm = next(row for row in sample["algorithms"] if row["algorithm_id"] == "bam-qc")
    metric = next(row for row in algorithm["metrics"] if row["metric_id"] == "total_reads")
    for other in sample["algorithms"]:
        other["metrics"] = []
    algorithm["metrics"] = [metric] * rows
    return sample


def _library(**changes) -> Library:
    """The shipped library with entry fields (``{(alg, metric): {field: value}}``) or licenses replaced."""

    library = load_library()
    entries = dict(library.entries)
    for key, fields in changes.get("entries", {}).items():
        entries[key] = Entry(key[0], key[1], {**entries[key].raw, **fields})
    licenses = {**library.licenses, **changes.get("licenses", {})}
    catalog = {key: dict(value) for key, value in library.method_catalog.items()}
    for key, fields in changes.get("catalog", {}).items():
        catalog[key] = {**catalog[key], **fields}
    return dataclasses.replace(library, entries=entries, licenses=licenses, method_catalog=catalog)


@pytest.mark.parametrize("case", ("rows", "bytes"))
def test_over_limit_report_is_refused_without_a_file(ran_root, tmp_path, capsys, monkeypatch, frozen, case) -> None:
    sample = _expanded(5001 if case == "rows" else 4000)
    library = load_library()
    if case == "bytes":
        # Under the row limit, over the 5 MiB byte limit.
        library = _library(entries={("bam-qc", "total_reads"): {"confounders": ["Long confounder text. " * 60]}})
        with pytest.raises(ReportRefused, match="5 MiB"):
            build_html(sample, FROZEN_AT, report_scrubber(tmp_path), library)
        monkeypatch.setattr(report_command, "load_library", lambda: library)
        monkeypatch.setattr(LibraryHolder, "get", lambda self: library)
    monkeypatch.setattr(LabApi, "sample", lambda self, id12: sample)
    out = tmp_path / "big.html"
    code, payload = _report(capsys, "--html", sample["id12"], "--root", str(ran_root), "--out", str(out))
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-034"
    assert ("5,000 row" if case == "rows" else "5 MiB") in payload["data"]["cause"]
    assert not out.exists() and not list(tmp_path.glob("*part-*"))


def test_exactly_the_row_limit_is_written() -> None:
    report, document = build_html(_expanded(5000), FROZEN_AT, report_scrubber(Path("/nonexistent")))
    assert len(report["metrics"]) == 5000
    assert len(document) <= 5 * 1024 * 1024


def test_unavailable_or_empty_samples_are_refused() -> None:
    scrubber = report_scrubber(Path("/nonexistent"))
    with pytest.raises(ReportRefused, match="unavailable"):
        project_report({**_reportable_sample(), "state": "unreadable"}, FROZEN_AT, scrubber)
    with pytest.raises(ReportRefused, match="no eligible scalar"):
        project_report(_expanded(0), FROZEN_AT, scrubber)


def test_curated_text_is_scrubbed_not_filtered(tmp_path) -> None:
    sample = _reportable_sample()
    root = tmp_path / "root"
    body = "A long curated license body. " * 70
    library = _library(
        licenses={"gpl-copyleft": {**load_library().licenses["gpl-copyleft"], "body": body}},
        catalog={"bam-qc": {
            "question": f"What does {root}/lab/x show?",
            "limitations": "Trade secret law in Slovakia applies to sk-adjacent prose.",
            "requirements": tuple(f"Curated requirement {n}: risk-adjusted, task-specific, tokenized."
                                  for n in range(15)),
        }},
    )
    for license in sample["licenses"]:
        if license["license_id"] == "gpl-copyleft":
            license["body"] = body
    assert len(body) > 2000
    _, document = build_html(sample, FROZEN_AT, report_scrubber(root), library)
    text = document.decode()
    assert str(root) not in text and "&lt;ROOT&gt;/lab/x" in text
    for n in range(15):
        assert f"Curated requirement {n}: risk-adjusted, task-specific, tokenized." in text
    assert "Trade secret law in Slovakia applies to sk-adjacent prose." in text
    assert html.escape(body.strip()) in text
    assert "withheld" not in text


def test_library_free_text_is_withheld_only_when_it_looks_private(tmp_path) -> None:
    prose = ["risk-adjusted", "task-specific", "Slovakia", "trade secret", "tokenized", "Bearer of news",
             "log2/GC slope", "and/or", "reads / 1M unmapped primaries", "N/A", "5'/3' ends", "ratio 1:2"]
    private = ["/Users/someone/run.bam", "AKIA" + "ABCDEFGHIJKLMNOP", "sk-" + "a1B2c3D4e5F6g7H8i9", "password=hunter2",
               "read " + "ACGT" * 8, "f" * 40, "`/data/donors/Smith_J/run.bam`", "path=/Volumes/JDoe/x.bam",
               "see,/mnt/runs/x", "[/srv/data/run]", "~/runs/x.bam", "C:/Users/JDoe/run.bam", "D:\\runs",
               "file:///tmp/x.bam", "FILE://host/share", "read 3f9a1c2b-7e04-4c1d-9a2b-0123456789ab"]
    library = _library(entries={
        ("bam-qc", "total_reads"): {"confounders": prose + private, "label": "Primary reads, risk-adjusted"},
        ("bam-qc", "mapped_fraction"): {"label": "Mapped share (lab_loo_v1 policy, Lab-curated)"},
        ("bam-qc", "mean_read_q"): {"label": ""},
    })
    report, document = build_html(_reportable_sample(), FROZEN_AT, report_scrubber(tmp_path), library)
    text = document.decode()
    row = next(row for row in report["metrics"] if row["metric_id"] == "total_reads")
    assert row["label"] == "Primary reads, risk-adjusted"
    assert row["confounders"] == "; ".join(prose) + f"; [{len(private)} items withheld]"
    mapped = next(row for row in report["metrics"] if row["metric_id"] == "mapped_fraction")
    # Labels are curated: shown as recorded, never filtered or word-redacted.
    assert mapped["label"] == "Mapped share (lab_loo_v1 policy, Lab-curated)"
    unlabelled = next(row for row in report["metrics"] if row["metric_id"] == "mean_read_q")
    assert unlabelled["label"] == "mean_read_q (label not recorded)"
    assert WITHHELD_TEXT in report["limitations"]
    for secret in private:
        assert html.escape(secret) not in text
    assert f"[{len(private)} items withheld]" in text


@pytest.mark.parametrize("host", ("clia", "root", "lab.example.org"))
def test_host_names_are_scrubbed_from_library_text_only(tmp_path, monkeypatch, host) -> None:
    monkeypatch.setattr(report_command, "_hostname", lambda: host)
    word = host.split(".")[0]
    root = tmp_path / "root"
    sample = _reportable_sample()
    for algorithm in sample["algorithms"]:
        algorithm["provenance"]["tool"] = word
    library = _library(
        entries={("bam-qc", "total_reads"): {
            "confounders": [f"Library {word.upper()} note for {getpass.getuser()}", "Labels stay",
                            f"{word}_loo_v1 stays"],
            "label": f"Primary reads ({word.upper()} {word} label)"}},
        catalog={"bam-qc": {"question": f"Curated: {word.upper()} and {word} stay; {root}/lab/x is ROOT."}},
    )
    report, document = build_html(sample, FROZEN_AT, report_scrubber(root), library)
    text = document.decode()
    # Curated repo text and the CLIA explainer are never word-redacted.
    assert f"Curated: {word.upper()} and {word} stay; &lt;ROOT&gt;/lab/x is ROOT." in text
    assert "What CLIA means for this demo" in text and "<strong>CLIA</strong> (1988)" in text
    assert '<p class="flag">FLAG: CLIA</p>' in text
    # Library free text loses the host word and the login.
    total = next(row for row in report["metrics"] if row["metric_id"] == "total_reads")
    assert total["confounders"] == f"Library <redacted> note for <redacted>; Labels stay; {word}_loo_v1 stays"
    assert total["label"] == f"Primary reads ({word.upper()} {word} label)"
    # Validated identifiers are never rewritten.
    assert f"Tool: {word};" in text and all(row["tool"] == word for row in report["metrics"])
    scrub = report_scrubber(root)
    assert scrub.redact_words(f"<ROOT> <path> <redacted> {word}") == "<ROOT> <path> <redacted> <redacted>"
    assert report_command._host_pattern("ab") is None


def test_root_and_home_are_replaced_only_as_whole_path_prefixes(tmp_path, monkeypatch) -> None:
    scrub = dataclasses.replace(report_scrubber(Path("/data/lab")), home="/data", login=None, host=None)
    assert scrub.curated("/data/lab/x and /data/lab") == "<ROOT>/x and <ROOT>"
    assert scrub.curated("/data/lab2/Smith_J") == "<redacted>/lab2/Smith_J"
    assert scrub.curated("input/data and my/data/lab and data.lab") == "input/data and my/data/lab and data.lab"
    assert scrub.curated("(/data/lab), '/data'") == "(<ROOT>), '<redacted>'"
    assert scrub.curated("/database/x") == "/database/x"


def test_served_catalogue_text_is_replaced_by_the_curated_catalogue() -> None:
    sample = _reportable_sample()
    row = next(row for row in sample["catalog"] if row["algorithm_id"] == "bam-qc")
    row["question"] = "Served /private/donor/question"
    report, document = build_html(sample, FROZEN_AT, report_scrubber(Path("/nonexistent")))
    text = document.decode()
    curated = load_library().method_catalog["bam-qc"]["question"]
    assert report["methods"]["bam-qc"]["question"] == curated and html.escape(curated) in text
    assert "/private/donor" not in text and report["methods"]["bam-qc"]["catalog_differs"] is True
    assert "differed from the curated method catalogue" in text


def test_empty_clia_copy_and_unreadable_out_parent_are_refused(ran_root, tmp_path, capsys, monkeypatch, frozen) -> None:
    from traceback_lab.web import server

    original = server.clia_copy
    monkeypatch.setattr(server, "clia_copy", lambda: {**original(), "short": "  \n"})
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(tmp_path / "a.html"))
    assert code == 3 and "CLIA explainer" in payload["data"]["cause"]
    monkeypatch.setattr(server, "clia_copy", original)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root),
                                "--out", str(locked / "inner" / "r.html"))
    finally:
        locked.chmod(0o700)
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-034"
    assert "could not be" in payload["data"]["cause"]


def test_minute_precision_instants_are_accepted_and_impossible_dates_refused() -> None:
    scrubber = report_scrubber(Path("/nonexistent"))
    sample = _reportable_sample()
    sample["run_utc"] = sample["completed_utc"] = "2026-10-19T11:00Z"
    assert project_report(sample, FROZEN_AT, scrubber)["recorded_utc_kind"] == "completion"
    sample["run_utc"] = sample["completed_utc"] = "2026-10-19T11:00:00.250Z"
    assert project_report(sample, FROZEN_AT, scrubber)["recorded_utc_kind"] == "completion"
    for bad in ("2026-02-30T11:00:00Z", "2026-10-19T11:00.5Z", "2026-10-19T11:60Z", "2026-10-19 11:00Z"):
        sample["run_utc"] = sample["completed_utc"] = bad
        with pytest.raises(ReportRefused):
            project_report(sample, FROZEN_AT, scrubber)


@pytest.mark.parametrize("error", (OSError, ValueError, TypeError, AttributeError, IndexError, KeyError))
def test_unreadable_clia_copy_is_refused(ran_root, tmp_path, capsys, monkeypatch, frozen, error) -> None:
    from traceback_lab.web import server

    def broken():
        raise error("gone")

    monkeypatch.setattr(server, "clia_copy", broken)
    out = tmp_path / "r.html"
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-034"
    assert "CLIA explainer" in payload["data"]["cause"]
    assert not out.exists()


def test_a_file_that_appears_after_the_check_is_never_replaced(ran_root, tmp_path, capsys, monkeypatch, frozen) -> None:
    out = tmp_path / "race.html"
    real_link = os.link

    def racing(source, target):
        Path(target).write_text("someone else's file")
        return real_link(source, target)

    monkeypatch.setattr(report_command.os, "link", racing)
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-034" and payload["data"]["exists"] is True
    assert out.read_text() == "someone else's file"
    assert not list(tmp_path.glob("*part-*"))


def test_staging_is_short_random_and_a_collision_is_not_reported_as_exists(
        ran_root, tmp_path, capsys, monkeypatch, frozen) -> None:
    out = tmp_path / ("r" * 250 + ".html")
    monkeypatch.setattr(report_command.secrets, "token_hex", lambda n: "0" * (2 * n))
    staging = tmp_path / f".traceback-report-{os.getpid()}-{'0' * 16}.part"
    staging.write_text("another writer's staging")
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-034"
    assert "exists" not in payload["data"] and "could not be written" in payload["data"]["cause"]
    assert staging.read_text() == "another writer's staging"
    assert not out.exists()
    staging.unlink()
    monkeypatch.undo()
    monkeypatch.setattr(report_command, "_utcnow", lambda: FROZEN_AT)
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 0 and out.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [out.name]


def test_filesystems_without_hard_links_get_an_exclusive_create(ran_root, tmp_path, capsys, monkeypatch, frozen) -> None:
    def unsupported(source, target):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(report_command.os, "link", unsupported)
    out = tmp_path / "exfat.html"
    code, payload = _report(capsys, "--html", _id12(ran_root), "--root", str(ran_root), "--out", str(out))
    assert code == 0
    assert hashlib.sha256(out.read_bytes()).hexdigest() == payload["data"]["sha256"]
    assert not list(tmp_path.glob("*part-*"))


def test_restriction_copy_matches_the_browser_export() -> None:
    source = REPORT_JS.read_text()
    for flag, text in RESTRICTION_FLAG_TEXT.items():
        assert f'    {flag}: "{text}",' in source
    for text in (report_command.VIRAL_PRIVACY_TEXT, CLINICAL_ADVICE_WARNING, report_command.INCOMPLETE_RESTRICTIONS,
                 *report_command.FIXED_LIMITATIONS):
        assert text in source
    assert "const MAX_ROWS = 5000;" in source and "const MAX_BYTES = 5 * 1024 * 1024;" in source


def _metric(sample: dict, algorithm_id: str, metric_id: str) -> dict:
    algorithm = next(row for row in sample["algorithms"] if row["algorithm_id"] == algorithm_id)
    return next(row for row in algorithm["metrics"] if row["metric_id"] == metric_id)


def _algorithm(sample: dict, algorithm_id: str) -> dict:
    return next(row for row in sample["algorithms"] if row["algorithm_id"] == algorithm_id)


def _parity_cases() -> dict[str, tuple[dict, list[str]]]:
    """Named samples, each with the ``algorithm/metric`` keys whose value becomes Infinity."""

    cases: dict[str, tuple[dict, list[str]]] = {}
    base = _reportable_sample()
    base["completed_utc"] = base["run_utc"]
    _metric(base, "sex-check", "call")["value"] = "female"
    _metric(base, "copy-number", "pon_policy")["value"] = "unlisted-policy"
    _metric(base, "copy-number", "identifiable")["value"] = True
    cases["base"] = (base, [])

    licenses = copy_of(base)
    licenses["licenses"] = [row for row in licenses["licenses"] if row["license_id"] != "gpl-copyleft"]
    licenses["licenses"][0] = {**licenses["licenses"][0], "body": "changed body"}
    licenses["licenses"].append({"license_id": "unknown-license", "headline": "x", "body": "y"})
    licenses["licenses"].append(dict(licenses["licenses"][1]))
    _metric(licenses, "bam-qc", "total_reads")["entry"]["flags"] = ["CLIA"]
    cell = _algorithm(licenses, "cell-origin")
    cell["algorithm_version"] = "1"
    cases["licenses"] = (licenses, [])

    no_licenses = copy_of(base)
    del no_licenses["licenses"]
    cases["no_licenses"] = (no_licenses, [])

    selection = copy_of(base)
    selection["unreadable_results"] = ["bam-qc", "not-a-method"]
    selection["ambiguous_results"] = ["copy-number", "bam-qc"]
    next(row for row in selection["catalog"] if row["algorithm_id"] == "sex-check")["status"] = "not run"
    selection["malformed_result_selections"] = True
    cases["selection"] = (selection, [])

    missing_selection = copy_of(base)
    for key in ("unreadable_results", "ambiguous_results", "malformed_result_selections", "excluded_chunks",
                "completed_utc"):
        missing_selection.pop(key, None)
    cases["missing_selection"] = (missing_selection, [])

    methods = copy_of(base)
    skipped = _algorithm(methods, "copy-number")
    skipped.update(status="skipped", skip_reason="depth")
    failed = _algorithm(methods, "sex-check")
    failed.update(status="failed")
    odd = _algorithm(methods, "fragment-size")
    odd.update(status="skipped", skip_reason="private reason text")
    _algorithm(methods, "bam-qc")["provenance"]["tool"] = "/private/tool"
    methods["algorithms"] = [row for row in methods["algorithms"] if row["algorithm_id"] != "cell-origin"]
    catalog_cell = next(row for row in methods["catalog"] if row["algorithm_id"] == "cell-origin")
    catalog_cell.update(status="skipped", skip_reason="missing_asset")
    cases["methods"] = (methods, [])

    values = copy_of(base)
    _metric(values, "bam-qc", "total_reads")["unit"] = "furlongs"
    _metric(values, "bam-qc", "mapped_fraction")["state"] = "bogus_state"
    _metric(values, "bam-qc", "mapq_ge20_fraction").update(state="in_range", reason="no_pon")
    _metric(values, "bam-qc", "mean_read_q").update(state="not_interpretable", reason="no_pon", n=-1)
    _metric(values, "bam-qc", "duplicate_fraction")["n"] = 12
    _metric(values, "bam-qc", "gc_bias_slope")["value"] = "text"
    _metric(values, "bam-qc", "supplementary_fraction")["unit"] = "reads"  # a known unit, but not this metric's
    _metric(values, "fragment-size", "mode_bp")["unit"] = None  # declared producer unit, payload None
    _algorithm(values, "bam-qc")["metrics"].append({"metric_id": "not_registered", "value": 1, "unit": "reads"})
    values["excluded_chunks"] = [3, -1, "x", True]
    cases["values"] = (values, ["bam-qc/genome_ge1x_fraction", "copy-number/tumor_fraction"])

    excluded = copy_of(base)
    excluded["excluded_chunks"] = [4, 7]
    excluded["run_utc"] = excluded["completed_utc"] = "2026-10-19T11:00Z"
    cases["excluded"] = (excluded, [])
    return cases


def copy_of(sample: dict) -> dict:
    return json.loads(json.dumps(sample))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_rows_and_limitations_match_the_browser_projection() -> None:
    cases = _parity_cases()
    script = r"""
      require(process.argv[1]);
      let input = '';
      process.stdin.on('data', (chunk) => { input += chunk; });
      process.stdin.on('end', () => {
        const out = {};
        for (const [name, [sample, infinite]] of Object.entries(JSON.parse(input))) {
          for (const key of infinite) {
            const [algorithmId, metricId] = key.split('/');
            const algorithm = sample.algorithms.find((row) => row.algorithm_id === algorithmId);
            algorithm.metrics.find((row) => row.metric_id === metricId).value = Infinity;
          }
          try {
            out[name] = globalThis.TracebackLabReport.project(sample, '2026-11-07T12:00:00Z',
              (m) => ({displayValue: m.value == null ? 'unavailable' : String(m.value), displayUnit: m.unit}));
          } catch (error) { out[name] = {error: error.code}; }
        }
        process.stdout.write(JSON.stringify(out));
      });
    """
    js_all = json.loads(subprocess.run(["node", "-e", script, str(REPORT_JS)], input=json.dumps(cases),
                                       capture_output=True, text=True, check=True).stdout)
    keys = ("algorithm_id", "metric_id", "label", "raw_value", "raw_unit", "recorded_support_n", "status",
            "skip_reason", "interpretation_state", "interpretation_reason", "tool", "tool_version",
            "parameters_sha256", "attempt_id", "reference_id", "computed_at")
    for name, (sample, infinite) in cases.items():
        for key in infinite:
            _metric(sample, *key.split("/"))["value"] = float("inf")
        js = js_all[name]
        assert "error" not in js, (name, js)
        py = project_report(sample, FROZEN_AT, report_scrubber(Path("/nonexistent")))
        assert [[row[key] for key in keys] for row in py["metrics"]] == \
            [[row[key] for key in keys] for row in js["metrics"]], name
        assert py["limitations"] == js["limitations"], name
        for key in ("schema_version", "sample_id", "source_id", "full_input_id", "recorded_utc",
                    "recorded_utc_kind", "interpretation_version", "cohort_version", "example"):
            assert py[key] == js[key], (name, key)
    base = project_report(cases["base"][0], FROZEN_AT, report_scrubber(Path("/nonexistent")))
    exported = {(row["algorithm_id"], row["metric_id"]) for row in base["metrics"]}
    assert ("sex-check", "call") in exported and ("copy-number", "identifiable") in exported
    assert ("copy-number", "pon_policy") not in exported


def _every_entry_sample() -> dict:
    """Every visible library entry with a value its kind accepts and its producer unit."""

    sample = _reportable_sample()
    sample["completed_utc"] = sample["run_utc"]
    library = load_library()
    categories = {("sex-check", "call"): "male", ("copy-number", "pon_policy"): "lab_loo_v1"}
    for algorithm in sample["algorithms"]:
        for metric in algorithm["metrics"]:
            key = (algorithm["algorithm_id"], metric["metric_id"])
            kind = library.entry(*key).get("value_kind")
            if kind == "categorical":
                metric["value"] = categories.get(key, True)
            elif kind == "vector":
                metric["value"] = [1, 2]
            else:
                metric["value"] = 0.25
            metric["unit"] = report_command.audited_units()[key]
    return sample


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
@pytest.mark.parametrize("cell_origin_version", ("1", "2"))
def test_every_library_entry_matches_the_browser_rows_and_restrictions(cell_origin_version) -> None:
    library = load_library()
    sample = _every_entry_sample()
    _algorithm(sample, "cell-origin")["algorithm_version"] = cell_origin_version
    script = r"""
      require(process.argv[1]);
      let input = '';
      process.stdin.on('data', (chunk) => { input += chunk; });
      process.stdin.on('end', () => {
        const sample = JSON.parse(input), core = globalThis.TracebackLabReport;
        const model = core.project(sample, '2026-11-07T12:00:00Z',
          (m) => ({displayValue: m.value == null ? 'unavailable' : String(m.value), displayUnit: m.unit}));
        const versions = Object.fromEntries(sample.algorithms.map((row) => [row.algorithm_id, row.algorithm_version]));
        const restrictions = model.metrics.map((row) => {
          const r = core.restrictionsForMetric(row.algorithm_id, row.metric_id, versions[row.algorithm_id]);
          return {flags: r.flags, license_ids: r.licenseIds};
        });
        process.stdout.write(JSON.stringify({model, restrictions}));
      });
    """
    js = json.loads(subprocess.run(["node", "-e", script, str(REPORT_JS)], input=json.dumps(sample),
                                   capture_output=True, text=True, check=True).stdout)
    py = project_report(sample, FROZEN_AT, report_scrubber(Path("/nonexistent")))
    keys = ("algorithm_id", "metric_id", "label", "raw_value", "raw_unit", "recorded_support_n",
            "interpretation_state", "interpretation_reason")
    assert [[row[key] for key in keys] for row in py["metrics"]] == [[row[key] for key in keys] for row in js["model"]["metrics"]]
    assert py["limitations"] == js["model"]["limitations"]
    # modbase-qc/mod_codes is categorical with no category lab-report.js accepts, so both omit it.
    visible = {key for key in library.entries if key[0] not in library.hidden_algorithms
               and library.entries[key].get("value_kind") != "vector" and key != ("modbase-qc", "mod_codes")}
    exported = {(row["algorithm_id"], row["metric_id"]) for row in py["metrics"]}
    assert exported == {key for key in visible if key[0] in {row["algorithm_id"] for row in sample["algorithms"]}}
    assert len(exported) > 150

    def joined(value) -> str | None:
        items = value if isinstance(value, list) else [value] if value else []
        return "; ".join(items) or None

    for row, restriction in zip(py["metrics"], js["restrictions"]):
        assert row["flags"] == restriction["flags"], row["metric_id"]
        assert row["license_ids"] == restriction["license_ids"], row["metric_id"]
        entry = library.entry(row["algorithm_id"], row["metric_id"])
        reference = entry.get("reference") or {}
        expected_reference = ": ".join(str(part) for part in (reference.get("kind"), reference.get("source")) if part)
        assert row["reference"] == (expected_reference or None)
        steps = entry.get("next_steps") or {}
        assert row["confounders"] == joined(entry.get("confounders"))
        assert row["analytical_next_steps"] == joined(steps.get("analytical"))
        assert row["clinical_next_steps"] == joined(steps.get("clinical_setting"))
    assert not any("withheld" in str(value) for row in py["metrics"] for value in row.values())
    modkit = any("modkit-ont-public" in row["license_ids"] for row in py["metrics"])
    assert modkit is (cell_origin_version == "1")


def test_units_vocabulary_matches_the_browser() -> None:
    source = REPORT_JS.read_text()
    block = source.split("  const units = new Set([", 1)[1].split("]);", 1)[0]
    assert set(re.findall(r'"([^"]+)"', block)) == set(report_command.UNITS)


def test_unknown_library_flag_or_license_is_disclosed_as_incomplete() -> None:
    shipped = load_library()

    def consistent() -> dict:
        # Payload restriction metadata that agrees with the library everywhere.
        sample = _every_entry_sample()
        for algorithm in sample["algorithms"]:
            for metric in algorithm["metrics"]:
                flags, licenses = report_command._restrictions(
                    shipped, algorithm["algorithm_id"], metric["metric_id"], algorithm.get("algorithm_version"))
                metric["entry"] = {**metric["entry"], "flags": flags, "license_ids": licenses}
        sample["licenses"] = [{"license_id": key, **value} for key, value in shipped.licenses.items()]
        return sample

    library = _library(entries={("bam-qc", "total_reads"): {"flags": ["CLIA", "NOT_A_FLAG"]}})
    sample = consistent()
    _metric(sample, "bam-qc", "total_reads")["entry"]["flags"] = ["CLIA", "NOT_A_FLAG"]
    baseline = project_report(consistent(), FROZEN_AT, report_scrubber(Path("/nonexistent")))
    report = project_report(sample, FROZEN_AT, report_scrubber(Path("/nonexistent")), library)
    assert report_command.INCOMPLETE_RESTRICTIONS not in baseline["limitations"]
    assert report_command.INCOMPLETE_RESTRICTIONS in report["limitations"]
    row = next(row for row in report["metrics"] if row["metric_id"] == "total_reads")
    assert row["flags"] == [RESTRICTION_FLAG_TEXT["CLIA"]]
    assert "NOT_A_FLAG" not in json.dumps(report)


def test_clia_explainer_is_the_page_copy() -> None:
    _, document = build_html(_reportable_sample(), FROZEN_AT, report_scrubber(Path("/nonexistent")))
    text = document.decode()
    short = clia_copy()["short"]
    first = short.split("\n\n", 1)[0].replace("**CLIA**", "<strong>CLIA</strong>").replace("'", "&#x27;")
    assert f"<p>{first}</p>" in text
    assert '<span class="flag-pill">FLAG: LEGAL — verify</span>' in text


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - silence the test server
        pass


@needs_chrome
def test_report_renders_its_metrics_table_with_javascript_disabled(tmp_path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    report, document = build_html(_reportable_sample(), FROZEN_AT, report_scrubber(tmp_path))
    (site / "report.html").write_bytes(document)
    # A probe page proves the browser really runs with scripts disabled.
    (site / "probe.html").write_text("<!DOCTYPE html><html><body><script>document.body.dataset.ran='1'</script>"
                                     "<p id='probe'>probe</p></body></html>")
    handler = lambda *args, **kwargs: _Quiet(*args, directory=str(site), **kwargs)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    observed: dict = {}
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        profile = tmp_path / "profile"

        def inspect(command, evaluate) -> None:
            observed["table"] = evaluate("""(() => {
              const table = document.querySelector('#metrics-table');
              const box = table.getBoundingClientRect();
              const style = getComputedStyle(table);
              const rows = [...table.querySelectorAll('tbody tr')];
              return {width: box.width, height: box.height, display: style.display,
                visibility: style.visibility, rows: rows.length,
                firstRowHeight: rows[0].getBoundingClientRect().height,
                flag: document.querySelector('#clia-flag').getBoundingClientRect().height,
                footer: document.querySelector('#frozen').innerText,
                scripts: document.scripts.length};
            })()""")
            command("Page.navigate", {"url": f"{base}/probe.html"})
            # Wait from Python: with scripts disabled, page timers never fire, so a
            # DevTools-side setTimeout promise would hang until the socket timeout.
            deadline = time.monotonic() + 30
            while not evaluate("document.readyState === 'complete' && !!document.querySelector('#probe')"):
                assert time.monotonic() < deadline, "the probe page did not load"
                time.sleep(0.1)
            observed["probe_ran"] = evaluate("document.body.dataset.ran || null")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--blink-settings=scriptEnabled=false", f"--user-data-dir={profile}", "--window-size=1280,800",
             f"{base}/report.html"],
            profile, ok=lambda dom: 'id="metrics-table"' in dom and "</footer>" in dom, timeout=90,
            interact=inspect,
        )
    finally:
        server.shutdown()
        server.server_close()
    table = observed["table"]
    assert observed["probe_ran"] is None
    assert table["scripts"] == 0
    assert table["display"] == "table" and table["visibility"] == "visible"
    assert table["width"] > 0 and table["height"] > 0 and table["firstRowHeight"] > 0
    assert table["rows"] == len(report["metrics"])
    assert table["flag"] > 0
    assert FROZEN_FOOTER in table["footer"]
