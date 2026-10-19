"""Pure report projection preserves scalar provenance and excludes unsafe payloads."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parents[2] / "traceback_lab/web/static/lab-report.js"


def _node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script, str(REPORT)], capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_report_shape_csv_protection_and_private_diagnostics() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport;
      const source = 'a'.repeat(64), input = 'b'.repeat(64);
      const sample = {
        found: true, id12: source.slice(0, 12), source_id: source, full_input_id: input,
        example: false, run_utc: '', interpretation_version: 'v1', cohort_version: null,
        catalog: [{algorithm_id: 'sex-check'}, {algorithm_id: 'copy-number'},
          {algorithm_id: 'fragment-size'}, {algorithm_id: 'hidden-step'}],
        algorithms: [
          {algorithm_id: 'sex-check', status: 'ok', hidden: false, skip_message: '/private/read-id',
            failure: {stderr_tail: ['/private/stderr']}, metrics: [
              {metric_id: 'call', label: 'Sex call', value: 'female', unit: null, state: 'no_reference'},
              {metric_id: 'unsafe', label: 'Read /private/sequence', value: 'AKIA-PRIVATE', unit: null},
              {metric_id: 'huge', label: 'Huge', value: Infinity, unit: 'fraction'},
              {metric_id: 'object', label: 'Object', value: {sequence: 'ACGT'}, unit: 'reads'},
              {metric_id: 'negative', label: 'Negative', value: -0.2, unit: 'fraction'}],
            provenance: {tool: 'lab', tool_version: '1.0', parameters_sha256: 'c'.repeat(64),
              input_id: input, source_id: source, reference_id: 'reference-v1',
              attempt_id: null, timestamp_utc: '2026-10-19T12:00:00Z'}},
          {algorithm_id: 'copy-number', status: 'skipped', skip_reason: 'depth',
            skip_message: '/private/skip', metrics: []},
          {algorithm_id: 'fragment-size', status: 'ok', metrics: [
              {metric_id: 'mode_bp', label: 'Mode', value: 167, unit: 'bp'}],
            provenance: {tool: '/private/tool'}},
          {algorithm_id: 'hidden-step', hidden: true, status: 'ok', metrics: [
              {metric_id: 'secret', label: 'Secret', value: 42, unit: 'reads'}]},
        ],
        artifacts: {read_id: '/private/artifact'},
      };
      const format = (metric) => ({displayValue: metric.value == null ? 'unavailable' : String(metric.value),
        displayUnit: metric.unit});
      const model = report.project(sample, '2026-10-19T16:00:00Z', format);
      const csv = report.csv(model), json = report.json(model);
      process.stdout.write(JSON.stringify({model, csv, json, filename: report.filename(model, 'csv')}));
    """)
    model = data["model"]
    assert list(model) == [
        "schema_version", "generated_at", "research_use_only", "example", "sample_id",
        "source_id", "full_input_id", "run_utc", "interpretation_version", "cohort_version",
        "limitations", "metrics",
    ]
    assert len(model["metrics"]) == 3
    assert model["metrics"][1]["raw_value"] is None
    assert model["metrics"][2]["raw_value"] == -0.2
    assert model["metrics"][0]["computed_at"] == "2026-10-19T12:00:00Z"
    assert model["run_utc"] is None and model["metrics"][0]["raw_value"] == "female"
    assert "nonfinite" in " ".join(model["limitations"])
    assert "Method copy-number: skipped (depth)" in " ".join(model["limitations"])
    assert data["csv"].startswith("schema_version,generated_at,")
    assert "\r\n" in data["csv"] and "'\"-0.2\"" not in data["csv"]
    assert "\"'-0.2\"" in data["csv"]  # display text is formula-protected
    assert data["filename"] == "traceback-research-aaaaaaaaaaaa-20261019T160000Z.csv"
    for private in ("/private", "AKIA-PRIVATE", "ACGT", "hidden-step"):
        assert private not in data["json"] and private not in data["csv"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_report_rejects_identity_empty_model_and_row_or_byte_limits() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport;
      const source = 'a'.repeat(64), input = 'b'.repeat(64);
      const metric = {metric_id: 'count', label: 'Count', value: 1, unit: 'reads', state: null};
      const algorithm = {algorithm_id: 'depth', status: 'ok', hidden: false, metrics: [metric],
        provenance: {tool: 'lab', tool_version: '1', parameters_sha256: 'c'.repeat(64),
          input_id: input, source_id: source, reference_id: 'ref', attempt_id: null,
          timestamp_utc: '2026-10-19T12:00:00Z'}};
      const sample = {found: true, id12: source.slice(0, 12), source_id: source,
        full_input_id: input, example: true, run_utc: '', interpretation_version: 'v1',
        cohort_version: null, catalog: [{algorithm_id: 'depth'}], algorithms: [algorithm]};
      const format = (m) => ({displayValue: String(m.value), displayUnit: m.unit});
      const code = (fn) => { try { fn(); return null; } catch (error) { return error.code; } };
      const invalid = code(() => report.project({...sample, full_input_id: '/private'},
        '2026-10-19T16:00:00Z', format));
      const empty = code(() => report.project({...sample, algorithms: []},
        '2026-10-19T16:00:00Z', format));
      const boundary = report.project({...sample, algorithms: [{...algorithm,
        metrics: Array.from({length: 5000}, () => metric)}]}, '2026-10-19T16:00:00Z', format);
      const rowLimit = code(() => report.project({...sample, algorithms: [{...algorithm,
        metrics: Array.from({length: 5001}, () => metric)}]}, '2026-10-19T16:00:00Z', format));
      const large = report.project(sample, '2026-10-19T16:00:00Z',
        () => ({displayValue: 'x'.repeat(119), displayUnit: 'reads'}));
      const fake = code(() => report.csv({...large}));
      process.stdout.write(JSON.stringify({invalid, empty, boundary: boundary.metrics.length,
        rowLimit, fake, frozen: Object.isFrozen(large.metrics[0])}));
    """)
    assert data == {
        "invalid": "invalid_report_identity", "empty": "no_report_rows", "boundary": 5000,
        "rowLimit": "report_too_many_rows", "fake": "invalid_report_model", "frozen": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_tenfold_fixture_and_serialized_byte_limits() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport;
      const source = 'a'.repeat(64), input = 'b'.repeat(64);
      const metric = {metric_id: 'mode_bp', label: 'Mode', value: 167, unit: 'bp'};
      const provenance = {tool: 't'.repeat(120), tool_version: 'v'.repeat(40),
        parameters_sha256: 'c'.repeat(64), input_id: input, source_id: source,
        reference_id: 'r'.repeat(128), timestamp_utc: '2026-10-19T12:00:00Z'};
      const algorithm = {algorithm_id: 'fragment-size', status: 'ok',
        metrics: Array.from({length: 10}, () => metric), provenance};
      const sample = {found: true, id12: source.slice(0, 12), source_id: source,
        full_input_id: input, example: true, run_utc: '', interpretation_version: 'v1',
        cohort_version: null, catalog: [{algorithm_id: 'fragment-size'}], algorithms: [algorithm]};
      const format = (m) => ({displayValue: String(m.value), displayUnit: m.unit});
      const small = report.project(sample, '2026-10-19T16:00:00Z', format);
      const bigMetric = {...metric, label: 'L'.repeat(160)};
      const bigSample = {...sample, algorithms: [{...algorithm,
        metrics: Array.from({length: 5000}, () => bigMetric)}]};
      const large = report.project(bigSample, '2026-10-19T16:00:00Z',
        () => ({displayValue: 'x'.repeat(120), displayUnit: 'reads'}));
      const code = (fn) => { try { fn(); return null; } catch (error) { return error.code; } };
      process.stdout.write(JSON.stringify({rows: small.metrics.length,
        json: code(() => report.json(large)), csv: code(() => report.csv(large)),
        compactBytes: new TextEncoder().encode(JSON.stringify(large)).length}));
    """)
    assert data["rows"] == 10
    assert data["compactBytes"] < 5 * 1024 * 1024
    assert data["json"] == data["csv"] == "report_too_large"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_current_visible_metric_fixture_and_tenfold_scale() -> None:
    from tests.lab.test_acceptance_synthetic_routes import _sample

    sample = _sample()
    # The view fixture uses a presentation-only ID12 unrelated to its SHA source.
    sample["id12"] = sample["source_id"][:12]
    script = r"""
      require(process.argv[1]);
      let input = '';
      process.stdin.on('data', (chunk) => { input += chunk; });
      process.stdin.on('end', () => {
        const sample = JSON.parse(input), report = globalThis.TracebackLabReport;
        const format = (m) => ({displayValue: m.value == null ? 'unavailable' : String(m.value),
          displayUnit: m.unit});
        const current = report.project(sample, '2026-10-19T16:00:00Z', format);
        const expanded = {...sample, algorithms: sample.algorithms.map((algorithm) => ({
          ...algorithm, metrics: Array.from({length: 10}, () => algorithm.metrics).flat(),
        }))};
        const tenfold = report.project(expanded, '2026-10-19T16:00:00Z', format);
        process.stdout.write(JSON.stringify({current: current.metrics.length,
          tenfold: tenfold.metrics.length, jsonBytes: new TextEncoder().encode(report.json(tenfold)).length,
          csvBytes: new TextEncoder().encode(report.csv(tenfold)).length}));
      });
    """
    result = subprocess.run(
        ["node", "-e", script, str(REPORT)], input=json.dumps(sample),
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["current"] > 100
    assert data["tenfold"] == data["current"] * 10
    assert data["jsonBytes"] < 5 * 1024 * 1024
    assert data["csvBytes"] < 5 * 1024 * 1024
