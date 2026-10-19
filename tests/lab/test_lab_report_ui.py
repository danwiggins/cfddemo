"""Fresh report capture uses one selected-sample GET and one immutable output model."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome


def _reportable_sample() -> dict:
    sample = _sample()
    sample["source_id"] = ID12 + "c" * (64 - len(ID12))
    for algorithm in sample["algorithms"]:
        algorithm["provenance"]["source_id"] = sample["source_id"]
    sample["catalog"][0]["description"] = "Private data at /private/read-identifiers"
    next(algorithm for algorithm in sample["algorithms"] if algorithm["metrics"])["metrics"][0]["entry"][
        "confounders"
    ] = ["/private/read-identifiers"]
    return sample


@needs_chrome
def test_report_capture_download_print_and_selection_invalidation(tmp_path) -> None:
    sample = _reportable_sample()
    observed = {}
    with _server(tmp_path, sample) as server:
        original = server.app.api.sample
        calls = []

        def counted(id12):
            calls.append(id12)
            return original(id12)

        server.app.api.sample = counted
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            before = len(calls)
            evaluate("""(() => {
              window.reportBlobs = [];
              window.reportFiles = [];
              window.reportPrint = null;
              window.reportPrintCalls = 0;
              const create = URL.createObjectURL.bind(URL);
              URL.createObjectURL = (blob) => {
                blob.text().then((value) => window.reportBlobs.push(value));
                return create(blob);
              };
              const click = HTMLAnchorElement.prototype.click;
              HTMLAnchorElement.prototype.click = function () {
                if (this.download) { window.reportFiles.push(this.download); return; }
                return click.call(this);
              };
              window.print = () => {
                window.reportPrintCalls += 1;
                const root = document.querySelector('#lab-report-print');
                window.reportPrint = {
                  marks: root.querySelector('.report-print-marks').textContent,
                  input: root.textContent.includes(window.TracebackLab.state.sample.full_input_id),
                  definition: root.textContent.includes('most frequent aligned reference span'),
                  count: root.querySelectorAll('.report-print-metric').length,
                  privateData: root.textContent.includes('/private/read-identifiers'),
                };
                dispatchEvent(new Event('afterprint'));
              };
            })()""")
            evaluate("document.querySelector('.report-export-panel button').click()")
            for _ in range(100):
                if evaluate("!!document.querySelector('.report-preview')"):
                    break
                time.sleep(0.05)
            assert evaluate("!!document.querySelector('.report-preview')")
            observed["calls_after_prepare"] = len(calls) - before
            observed["preview"] = evaluate("document.querySelector('.report-preview').textContent")
            evaluate("""(() => {
              const buttons = [...document.querySelectorAll('.report-actions button')];
              buttons[0].click(); buttons[1].click(); buttons[2].click();
            })()""")
            for _ in range(100):
                if evaluate("window.reportBlobs.length === 2"):
                    break
                time.sleep(0.05)
            observed["blobs"] = evaluate("window.reportBlobs")
            observed["files"] = evaluate("window.reportFiles")
            observed["print"] = evaluate("window.reportPrint")
            observed["calls_after_actions"] = len(calls) - before
            evaluate("""(() => {
              window.TracebackLab.state.sessionExpired = true;
              const buttons = [...document.querySelectorAll('.report-actions button')];
              buttons[0].click(); buttons[2].click();
            })()""")
            observed["after_session"] = evaluate("""({blobs: window.reportBlobs.length,
              printCalls: window.reportPrintCalls})""")
            evaluate("location.hash = '#/sample/ffffffffffff/findings'")
            for _ in range(100):
                if evaluate("!document.querySelector('.report-preview')"):
                    break
                time.sleep(0.05)
            observed["cleared"] = evaluate("!document.querySelector('.report-preview')")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="report-export-panel"' in dom,
            interact=inspect, timeout=120,
        )

    assert observed["calls_after_prepare"] == 1
    assert observed["calls_after_actions"] == 1
    assert "Freshly captured report" in observed["preview"]
    assert sample["full_input_id"] in observed["preview"]
    assert len(observed["files"]) == len(observed["blobs"]) == 2
    assert observed["files"][0].endswith(".json") and observed["files"][1].endswith(".csv")
    assert '"schema_version": "1"' in observed["blobs"][0]
    assert observed["blobs"][1].startswith("schema_version,generated_at,")
    assert observed["print"]["input"] and observed["print"]["definition"]
    assert observed["print"]["count"] > 0
    assert not observed["print"]["privateData"]
    assert "RESEARCH USE ONLY" in observed["print"]["marks"]
    assert observed["after_session"] == {"blobs": 2, "printCalls": 1}
    assert observed["cleared"]


@needs_chrome
def test_report_download_writes_json_and_csv_from_one_capture(tmp_path) -> None:
    sample = _reportable_sample()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def inspect(command, evaluate) -> None:
            command("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads)})
            evaluate("document.querySelector('.report-export-panel button').click()")
            for _ in range(100):
                if evaluate("!!document.querySelector('.report-preview')"):
                    break
                time.sleep(0.05)
            assert evaluate("!!document.querySelector('.report-preview')")
            evaluate("""(() => {
              const buttons = [...document.querySelectorAll('.report-actions button')];
              buttons[0].click(); buttons[1].click();
            })()""")
            for _ in range(100):
                if len(list(downloads.glob("*.json"))) == len(list(downloads.glob("*.csv"))) == 1:
                    break
                time.sleep(0.05)

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="report-export-panel"' in dom,
            interact=inspect, timeout=120,
        )

    json_files = list(downloads.glob("*.json"))
    csv_files = list(downloads.glob("*.csv"))
    assert len(json_files) == len(csv_files) == 1
    model = json.loads(json_files[0].read_text(encoding="utf-8"))
    csv = csv_files[0].read_bytes()
    assert model["sample_id"] == ID12 and model["metrics"]
    assert b"\r\n" in csv and csv.startswith(b"schema_version,generated_at,")
    assert model["generated_at"].split(".", 1)[0].replace("-", "").replace(":", "") in json_files[0].name
