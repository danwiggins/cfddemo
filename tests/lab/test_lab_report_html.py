"""``traceback lab report --html``: one self-contained, scrubbed, frozen research report file."""

from __future__ import annotations

import copy
import getpass
import hashlib
import http.server
import json
import re
import shutil
import socket
import subprocess
import threading
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
    ReportRefused,
    build_html,
    project_report,
    report_scrubber,
)
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
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
    assert not list(tmp_path.glob("*.part-*"))

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


def _expanded(rows: int, *, confounders: str | None = None) -> dict:
    sample = _reportable_sample()
    algorithm = next(row for row in sample["algorithms"] if row["algorithm_id"] == "bam-qc")
    metric = next(row for row in algorithm["metrics"] if row["metric_id"] == "total_reads")
    if confounders is not None:
        metric = copy.deepcopy(metric)
        metric["entry"]["confounders"] = [confounders]
    for other in sample["algorithms"]:
        other["metrics"] = []
    algorithm["metrics"] = [metric] * rows
    return sample


@pytest.mark.parametrize("case", ("rows", "bytes"))
def test_over_limit_report_is_refused_without_a_file(ran_root, tmp_path, capsys, monkeypatch, frozen, case) -> None:
    sample = _expanded(5001) if case == "rows" else _expanded(4000, confounders="Long confounder text. " * 54)
    if case == "bytes":
        # Under the row limit, over the 5 MiB byte limit.
        with pytest.raises(ReportRefused, match="5 MiB"):
            build_html(sample, FROZEN_AT, report_scrubber(tmp_path))
    monkeypatch.setattr(LabApi, "sample", lambda self, id12: sample)
    out = tmp_path / "big.html"
    code, payload = _report(capsys, "--html", sample["id12"], "--root", str(ran_root), "--out", str(out))
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-034"
    assert ("5,000 row" if case == "rows" else "5 MiB") in payload["data"]["cause"]
    assert not out.exists() and not list(tmp_path.glob("*.part-*"))


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


def test_free_text_and_strings_pass_the_scrubber(tmp_path) -> None:
    sample = _reportable_sample()
    root = tmp_path / "root"
    sample["catalog"][1]["question"] = f"What does {root}/lab/x show for {getpass.getuser()}?"
    metric = next(row for row in sample["algorithms"] if row["metrics"])["metrics"][0]
    metric["entry"]["confounders"] = ["read R0033892 ACGTACGTACGTACGTACGTACGTACGTACGT", "plain confounder"]
    _, document = build_html(sample, FROZEN_AT, report_scrubber(root))
    text = document.decode()
    assert "/private/read-identifiers" not in text
    assert str(root) not in text and "ACGTACGTACGT" not in text
    assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(getpass.getuser())}(?![A-Za-z0-9])", text)
    assert "plain confounder" in text


def test_restriction_copy_matches_the_browser_export() -> None:
    source = REPORT_JS.read_text()
    for flag, text in RESTRICTION_FLAG_TEXT.items():
        assert f'    {flag}: "{text}",' in source
    for text in (report_command.VIRAL_PRIVACY_TEXT, CLINICAL_ADVICE_WARNING, report_command.INCOMPLETE_RESTRICTIONS,
                 *report_command.FIXED_LIMITATIONS):
        assert text in source
    assert "const MAX_ROWS = 5000;" in source and "const MAX_BYTES = 5 * 1024 * 1024;" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_rows_and_limitations_match_the_browser_projection() -> None:
    sample = _reportable_sample()
    sample["completed_utc"] = sample["run_utc"]
    for algorithm in sample["algorithms"]:
        for metric in algorithm["metrics"]:
            if (algorithm["algorithm_id"], metric["metric_id"]) == ("sex-check", "call"):
                metric["value"] = "female"
            if (algorithm["algorithm_id"], metric["metric_id"]) == ("copy-number", "pon_policy"):
                metric["value"] = "unlisted-policy"
            if (algorithm["algorithm_id"], metric["metric_id"]) == ("copy-number", "identifiable"):
                metric["value"] = True
    script = r"""
      require(process.argv[1]);
      let input = '';
      process.stdin.on('data', (chunk) => { input += chunk; });
      process.stdin.on('end', () => {
        const model = globalThis.TracebackLabReport.project(JSON.parse(input), '2026-11-07T12:00:00Z',
          (m) => ({displayValue: m.value == null ? 'unavailable' : String(m.value), displayUnit: m.unit}));
        process.stdout.write(JSON.stringify(model));
      });
    """
    js = json.loads(subprocess.run(["node", "-e", script, str(REPORT_JS)], input=json.dumps(sample),
                                   capture_output=True, text=True, check=True).stdout)
    py = project_report(sample, FROZEN_AT, report_scrubber(Path("/nonexistent")))
    keys = ("algorithm_id", "metric_id", "label", "raw_value", "raw_unit", "recorded_support_n",
            "interpretation_state", "interpretation_reason")
    assert [[row[key] for key in keys] for row in py["metrics"]] == [[row[key] for key in keys] for row in js["metrics"]]
    assert py["limitations"] == js["limitations"]
    for key in ("schema_version", "sample_id", "source_id", "full_input_id", "recorded_utc", "recorded_utc_kind",
                "interpretation_version", "cohort_version", "example"):
        assert py[key] == js[key], key
    exported = {(row["algorithm_id"], row["metric_id"]) for row in py["metrics"]}
    assert ("sex-check", "call") in exported and ("copy-number", "identifiable") in exported
    assert ("copy-number", "pon_policy") not in exported


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
            command("Page.enable")
            command("Page.navigate", {"url": f"{base}/probe.html"})
            for _ in range(50):
                if evaluate("document.readyState === 'complete' && !!document.querySelector('#probe')"):
                    break
                command("Runtime.evaluate", {"expression": "new Promise(r => setTimeout(r, 100))",
                                             "awaitPromise": True})
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
