"""Fresh report capture uses one selected-sample GET and one immutable output model."""

from __future__ import annotations

import json
import re
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
    sample["example"] = True
    observed = {}
    with _server(tmp_path, sample) as server:
        original = server.app.api.sample
        calls = []

        def counted(id12):
            calls.append(id12)
            # The real API resolves the full source digest to its canonical ID.
            return original(ID12 if id12 == sample["source_id"] else id12)

        server.app.api.sample = counted
        profile = Path(tmp_path / "browser-profile")

        def inspect(command, evaluate) -> None:
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
                const marks = root.querySelector('.report-print-marks');
                const limits = root.querySelector('.report-print-limits');
                const visible = (node) => {
                  const bounds = node.getBoundingClientRect();
                  return bounds.width > 0 && bounds.height > 0;
                };
                window.reportPrint = {
                  marks: marks.textContent,
                  limits: limits.textContent,
                  marksVisible: visible(marks),
                  limitsVisible: visible(limits),
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
              buttons[0].click(); buttons[1].click();
            })()""")
            command("Emulation.setEmulatedMedia", {"media": "print"})
            evaluate("[...document.querySelectorAll('.report-actions button')][2].click()")
            command("Emulation.setEmulatedMedia", {"media": "screen"})
            for _ in range(100):
                if evaluate("window.reportBlobs.length === 2"):
                    break
                time.sleep(0.05)
            observed["blobs"] = evaluate("window.reportBlobs")
            observed["files"] = evaluate("window.reportFiles")
            observed["print"] = evaluate("window.reportPrint")
            observed["calls_after_actions"] = len(calls) - before
            full_source_route = f"#/sample/{sample['source_id']}/methods"
            evaluate(f"location.hash = {json.dumps(full_source_route)}")
            for _ in range(100):
                if evaluate("window.TracebackLab.state.renderedHash") == full_source_route:
                    break
                time.sleep(0.05)
            assert evaluate("window.TracebackLab.state.renderedHash") == full_source_route
            assert evaluate("document.querySelector('.report-preview')?.textContent") == observed["preview"]
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
            profile, ok=lambda dom: 'data-ready="1"' in dom and
            re.search(r'<section[^>]*class="[^"]*\breport-export-panel\b', dom) is not None,
            interact=inspect, timeout=120,
        )

    assert observed["calls_after_prepare"] == 1
    assert observed["calls_after_actions"] == 1
    assert "Freshly captured report" in observed["preview"]
    assert sample["full_input_id"] in observed["preview"]
    assert len(observed["files"]) == len(observed["blobs"]) == 2
    assert observed["files"][0].endswith(".json") and observed["files"][1].endswith(".csv")
    assert '"schema_version": "2"' in observed["blobs"][0]
    assert observed["blobs"][1].startswith("schema_version,generated_at,")
    assert observed["print"]["input"] and observed["print"]["definition"]
    assert observed["print"]["count"] > 0
    assert not observed["print"]["privateData"]
    assert "RESEARCH USE ONLY" in observed["print"]["marks"]
    assert "SYNTHETIC EXAMPLE" in observed["print"]["marks"]
    assert "artifacts and taxon detail omitted" in observed["print"]["limits"]
    assert observed["print"]["marksVisible"] and observed["print"]["limitsVisible"]
    assert observed["after_session"] == {"blobs": 2, "printCalls": 1}
    assert observed["cleared"]


@needs_chrome
def test_report_uses_healthy_selection_despite_listing_flags_and_keeps_error_status(tmp_path) -> None:
    sample = _reportable_sample()
    with _server(tmp_path, sample) as server:
        listing = server.app.api.samples
        server.app.api.samples = lambda: {
            **listing(),
            "completed_input_unavailable": True,
            "completion_history_unavailable": True,
        }
        selected_sample = server.app.api.sample
        calls = []

        def counted(id12):
            calls.append(id12)
            return selected_sample(id12)

        server.app.api.sample = counted
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            assert evaluate("window.TracebackLab.state.sample?.found === true")
            assert evaluate("window.TracebackLab.state.completedInputUnavailable === true")
            assert evaluate("window.TracebackLab.state.completionHistoryUnavailable === true")
            evaluate("window.reportStatusNode = document.querySelector('[data-report-status]')")
            before = len(calls)
            evaluate("document.querySelector('.report-export-panel button').click()")
            for _ in range(100):
                if evaluate("!!document.querySelector('.report-preview')"):
                    break
                time.sleep(0.05)
            assert evaluate("!!document.querySelector('.report-preview')")
            assert len(calls) - before == 1
            assert evaluate("document.querySelector('[data-report-status]').classList.contains('problem')") is False

            def unavailable(id12):
                calls.append(id12)
                return {"found": False, "candidates": []}

            server.app.api.sample = unavailable
            before = len(calls)
            evaluate("document.querySelector('.report-export-panel button').click()")
            for _ in range(100):
                if evaluate("document.querySelector('[data-report-status]')?.textContent.includes('could not be refreshed')"):
                    break
                time.sleep(0.05)
            assert len(calls) - before == 1
            assert evaluate("!document.querySelector('.report-preview')")
            assert evaluate("document.querySelector('[data-report-status]').classList.contains('problem')")
            assert evaluate("document.querySelector('[data-report-status]').getAttribute('role')") == "status"
            assert evaluate("window.reportStatusNode === document.querySelector('[data-report-status]')")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and
            re.search(r'<section[^>]*class="[^"]*\breport-export-panel\b', dom) is not None,
            interact=inspect, timeout=120,
        )


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
              window.reportDownloadPayloads = {};
              const create = URL.createObjectURL.bind(URL);
              URL.createObjectURL = (blob) => {
                const kind = blob.type.startsWith('application/json') ? 'json'
                  : blob.type.startsWith('text/csv') ? 'csv' : null;
                const url = create(blob);
                if (kind) blob.text().then((text) => { window.reportDownloadPayloads[kind] = text; });
                return url;
              };
            })()""")
            evaluate("""(() => {
              const buttons = [...document.querySelectorAll('.report-actions button')];
              buttons[0].click(); buttons[1].click();
            })()""")
            deadline = time.monotonic() + 30
            payloads = {}
            while time.monotonic() < deadline:
                if set(payloads) != {"json", "csv"}:
                    payloads = evaluate("window.reportDownloadPayloads")
                json_files = list(downloads.glob("*.json"))
                csv_files = list(downloads.glob("*.csv"))
                if (set(payloads) == {"json", "csv"} and len(json_files) == len(csv_files) == 1
                        and json_files[0].read_bytes() == payloads["json"].encode("utf-8")
                        and csv_files[0].read_bytes() == payloads["csv"].encode("utf-8")):
                    break
                time.sleep(0.05)
            else:
                sizes = {path.name: path.stat().st_size for path in downloads.iterdir()}
                expected = {kind: len(text.encode("utf-8")) for kind, text in payloads.items()}
                raise AssertionError(
                    f"Report downloads did not finish with captured Blob bytes: saved={sizes}, expected={expected}"
                )

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and
            re.search(r'<section[^>]*class="[^"]*\breport-export-panel\b', dom) is not None,
            interact=inspect, timeout=120,
        )

    json_files = list(downloads.glob("*.json"))
    csv_files = list(downloads.glob("*.csv"))
    assert len(json_files) == len(csv_files) == 1
    model = json.loads(json_files[0].read_text(encoding="utf-8"))
    csv = csv_files[0].read_bytes()
    assert model["schema_version"] == "2"
    assert all("recorded_support_n" in row for row in model["metrics"])
    columns = csv.split(b"\r\n", 1)[0].split(b",")
    assert len(columns) == 28 and columns[columns.index(b"raw_unit") + 1] == b"recorded_support_n"
    assert model["sample_id"] == ID12 and model["metrics"]
    assert b"\r\n" in csv and csv.startswith(b"schema_version,generated_at,")
    assert model["generated_at"].split(".", 1)[0].replace("-", "").replace(":", "") in json_files[0].name
