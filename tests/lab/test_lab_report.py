"""Pure report projection preserves scalar provenance and excludes unsafe payloads."""

from __future__ import annotations

import ast
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
          {algorithm_id: 'bam-qc'}, {algorithm_id: 'fragment-size'}, {algorithm_id: 'hidden-step'}],
        algorithms: [
          {algorithm_id: 'sex-check', status: 'ok', hidden: false, skip_message: '/private/read-id',
            failure: {stderr_tail: ['/private/stderr']}, metrics: [
              {metric_id: 'call', label: 'Plausible read ID R0033892', value: 'female', unit: null, state: 'no_reference'},
              {metric_id: 'unsafe', label: 'Looks like a valid metric', value: 42, unit: 'reads'},
              {metric_id: 'chry_chrx_ratio', label: 'Object', value: {sequence: 'ACGT'}, unit: 'ratio'}],
            provenance: {tool: 'lab', tool_version: '1.0', parameters_sha256: 'c'.repeat(64),
              input_id: input, source_id: source, reference_id: 'reference-v1',
              attempt_id: null, timestamp_utc: '2026-10-19T12:00:00Z'}},
          {algorithm_id: 'bam-qc', status: 'ok', metrics: [
              {metric_id: 'total_reads', label: 'Private credential credential123', value: Infinity, unit: 'reads'},
              {metric_id: 'gc_bias_slope', label: 'Private read R9988', value: -0.2, unit: 'log2/GC'}],
            provenance: {tool: 'lab', tool_version: '1.0', parameters_sha256: 'c'.repeat(64),
              input_id: input, source_id: source, reference_id: 'reference-v1',
              timestamp_utc: '2026-10-19T12:00:00Z'}},
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
    assert model["metrics"][0]["label"] == "Sex call (male / female / indeterminate)"
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
    for private in ("/private", "Plausible read ID R0033892", "credential123", "R9988",
                    "Looks like a valid metric", "ACGT", "hidden-step"):
        assert private not in data["json"] and private not in data["csv"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_missing_provenance_fields_and_unit_mismatch_omit_only_bad_rows() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport, source = 'a'.repeat(64), input = 'b'.repeat(64);
      const provenance = {tool: 'lab', tool_version: '1', parameters_sha256: 'c'.repeat(64),
        input_id: input, source_id: source, reference_id: 'ref',
        timestamp_utc: '2026-10-19T12:00:00Z'};
      const good = {algorithm_id: 'sex-check', status: 'ok', metrics: [
        {metric_id: 'call', label: 'Untrusted label', value: 'female', unit: null}], provenance};
      const bad = {algorithm_id: 'fragment-size', status: 'ok', metrics: [
        {metric_id: 'mode_bp', label: 'Untrusted label', value: 167, unit: 'bp'}], provenance};
      const sample = {found: true, id12: source.slice(0, 12), source_id: source,
        full_input_id: input, example: true, run_utc: '', interpretation_version: 'v1',
        cohort_version: null, catalog: [{algorithm_id: 'sex-check'},
          {algorithm_id: 'fragment-size'}], algorithms: [good, bad]};
      const format = (m) => ({displayValue: String(m.value), displayUnit: m.unit});
      const outcomes = {};
      for (const key of ['tool', 'tool_version', 'reference_id']) {
        const missing = {...provenance}; delete missing[key];
        const model = report.project({...sample, algorithms: [good, {...bad, provenance: missing}]},
          '2026-10-19T16:00:00Z', format);
        outcomes[key] = {rows: model.metrics.length, limitations: model.limitations.join(' | ')};
      }
      const mismatch = report.project({...sample, algorithms: [good, {...bad, metrics: [
        {...bad.metrics[0], unit: 'reads'}]}]}, '2026-10-19T16:00:00Z', format);
      process.stdout.write(JSON.stringify({outcomes, mismatch: {rows: mismatch.metrics.length,
        limitations: mismatch.limitations.join(' | ')}}));
    """)
    for key in ("tool", "tool_version", "reference_id"):
        assert data["outcomes"][key]["rows"] == 1
        assert "provenance was unavailable or invalid" in data["outcomes"][key]["limitations"]
        assert key not in data["outcomes"][key]["limitations"]
    assert data["mismatch"]["rows"] == 1
    assert "did not match audited producer metadata" in data["mismatch"]["limitations"]


def test_audited_metric_metadata_matches_library_and_static_producers() -> None:
    from traceback_lab.interpretation.engine import load_library
    from traceback_lab.registry import load_registry

    source = REPORT.read_text()
    audited = json.loads(source.split("  const audited = ", 1)[1].split(";\n  const sha =", 1)[0])
    library = load_library()
    visible = {row.id for row in load_registry() if row.id not in library.hidden_algorithms}
    expected = {(algorithm, metric): entry for (algorithm, metric), entry in library.entries.items()
                if algorithm in visible}
    actual = {(algorithm, metric): definition for algorithm, metrics in audited.items()
              for metric, definition in metrics.items()}
    assert set(actual) == set(expected)
    for key, definition in actual.items():
        assert definition[0] == expected[key]["label"]
        assert definition[2] == expected[key].get("value_kind")

    root = REPORT.parents[2] / "algorithms"
    for path in root.glob("*.py"):
        algorithm = path.stem.replace("_", "-")
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "metric" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            key = (algorithm, node.args[0].value)
            if key not in actual:
                continue
            unit = next((kw.value for kw in node.keywords if kw.arg == "unit"), None)
            if unit is None or isinstance(unit, ast.Constant):
                assert actual[key][1] == (unit.value if unit is not None else None), key


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_report_rejects_identity_empty_model_and_row_or_byte_limits() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport;
      const source = 'a'.repeat(64), input = 'b'.repeat(64);
      const metric = {metric_id: 'eligible_reads', label: 'Count', value: 1, unit: 'reads', state: null};
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
      const missingVersion = code(() => report.project({...sample, interpretation_version: undefined},
        '2026-10-19T16:00:00Z', format));
      const numericVersion = code(() => report.project({...sample, interpretation_version: 123},
        '2026-10-19T16:00:00Z', format));
      const numericCohort = code(() => report.project({...sample, cohort_version: 123},
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
      process.stdout.write(JSON.stringify({invalid, missingVersion, numericVersion,
        numericCohort, empty, boundary: boundary.metrics.length,
        rowLimit, fake, frozen: Object.isFrozen(large.metrics[0])}));
    """)
    assert data == {
        "invalid": "invalid_report_identity", "missingVersion": "invalid_report_identity",
        "numericVersion": "invalid_report_identity", "numericCohort": "invalid_report_identity",
        "empty": "no_report_rows", "boundary": 5000,
        "rowLimit": "report_too_many_rows", "fake": "invalid_report_model", "frozen": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_tenfold_fixture_and_serialized_byte_limits() -> None:
    data = _node(r"""
      require(process.argv[1]);
      const report = globalThis.TracebackLabReport;
      const source = 'a'.repeat(64), input = 'b'.repeat(64);
      const metric = {metric_id: 'microbial_rpm_primary', label: 'Untrusted label',
        value: 1, unit: 'estimated reads/million'};
      const provenance = {tool: 't'.repeat(120), tool_version: 'v'.repeat(40),
        parameters_sha256: 'c'.repeat(64), input_id: input, source_id: source,
        reference_id: 'r'.repeat(128), timestamp_utc: '2026-10-19T12:00:00Z'};
      const algorithm = {algorithm_id: 'microbial', status: 'ok',
        metrics: Array.from({length: 10}, () => metric), provenance};
      const sample = {found: true, id12: source.slice(0, 12), source_id: source,
        full_input_id: input, example: true, run_utc: '', interpretation_version: 'v1',
        cohort_version: null, catalog: [{algorithm_id: 'microbial'}], algorithms: [algorithm]};
      const format = (m) => ({displayValue: String(m.value), displayUnit: m.unit});
      const small = report.project(sample, '2026-10-19T16:00:00Z', format);
      const bigSample = {...sample, algorithms: [{...algorithm,
        metrics: Array.from({length: 5000}, () => metric)}]};
      const large = report.project(bigSample, '2026-10-19T16:00:00Z',
        () => ({displayValue: 'é'.repeat(6) + 'x'.repeat(114),
          displayUnit: 'estimated reads/million'}));
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
        const all = sample.algorithms.filter((algorithm) => !algorithm.hidden)
          .flatMap((algorithm) => algorithm.metrics.map((metric) =>
            `${algorithm.algorithm_id}/${metric.metric_id}`));
        const exported = new Set(current.metrics.map((metric) =>
          `${metric.algorithm_id}/${metric.metric_id}`));
        const omitted = all.filter((identity) => !exported.has(identity)).sort();
        const expanded = {...sample, algorithms: sample.algorithms.map((algorithm) => ({
          ...algorithm, metrics: Array.from({length: 10}, () => algorithm.metrics).flat(),
        }))};
        const tenfold = report.project(expanded, '2026-10-19T16:00:00Z', format);
        process.stdout.write(JSON.stringify({current: current.metrics.length, omitted,
          tenfold: tenfold.metrics.length, jsonBytes: new TextEncoder().encode(report.json(tenfold)).length,
          csvBytes: new TextEncoder().encode(report.csv(tenfold)).length}));
      });
    """
    result = subprocess.run(
        ["node", "-e", script, str(REPORT)], input=json.dumps(sample),
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["current"] == 182
    # The fixture assigns 1.0 to every metric, including producers whose real
    # values are booleans, approved categories, or vectors.
    assert data["omitted"] == [
        "copy-number/identifiable", "copy-number/pon_policy",
        "hypometh-reads/u_x_m_counts", "meth-tf/identifiable",
        "modbase-qc/mod_codes", "sex-check/call",
    ]
    assert data["tenfold"] == data["current"] * 10
    assert data["jsonBytes"] < 5 * 1024 * 1024
    assert data["csvBytes"] < 5 * 1024 * 1024
