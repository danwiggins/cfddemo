"""The browser's numeric display contract does not change stored measurements."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


FORMAT = Path(__file__).resolve().parents[2] / "traceback_lab/web/static/lab-format.js"


def test_formatter_asset_precedes_the_views() -> None:
    from traceback_lab.web.server import _assets

    assets = _assets()
    assert "javascript" in assets["/assets/lab-format.js"][0]
    page = assets["/"][1].decode()
    assert page.index('src="/assets/lab-format.js"') < page.index('src="/assets/lab.js"')


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable for pure browser formatter test")
def test_metric_display_units_roles_and_precision() -> None:
    script = r"""
      require(process.argv[2]);
      const {formatMetric, formatResultMetric} = globalThis.TracebackLabFormat;
      const cases = [
        [0, "fraction"], [1, "fraction"], [0.125, "fraction"], [1e-7, "fraction"],
        [-0.045, "fraction", {role: "difference"}], [0.045, "fraction"],
        [0.001, "ratio"], [1, "log2"], [12345, "reads", {valueKind: "count"}],
        [12345, "bp", {valueKind: "positive"}], [-0.0025, "log2/GC"],
        [null, "fraction", {reason: "no_eligible_calls"}], [NaN, "fraction"],
        [Infinity, "fraction"], [0, null], [false, null],
        [0.000999, "ratio"], [0.0009999, "ratio"], [0.001001, "ratio"],
        [Number.MAX_SAFE_INTEGER, "reads", {valueKind: "count"}],
        [Number.MAX_SAFE_INTEGER + 1, "reads", {valueKind: "count"}],
        [2.5, "reads", {valueKind: "count"}], [1e308, "fraction"],
      ];
      const source = {algorithm_id: "pmd-score", metric_id: "pmd_delta",
        value: -0.045, unit: "fraction", reason: null, entry: {value_kind: "signed"}};
      const undeclared = {algorithm_id: "probe", metric_id: "share",
        value: 0.5, unit: null, entry: {value_kind: "proportion"}};
      const categorical = {algorithm_id: "copy-number", metric_id: "identifiable",
        value: false, unit: null, entry: {value_kind: "categorical"}};
      const outputs = cases.map((args) => formatMetric(...args));
      const delta = formatResultMetric(source);
      const fragmentDelta = formatResultMetric({...source, algorithm_id: "frag-x-meth",
        metric_id: "short_fragment_hypomethylation_delta"});
      process.stdout.write(JSON.stringify({outputs, delta, fragmentDelta,
        undeclared: formatResultMetric(undeclared),
        categoricalFalse: formatResultMetric(categorical),
        categoricalText: formatResultMetric({...categorical, metric_id: "pon_policy", value: "none"}),
        categoricalNumber: formatResultMetric({...categorical, value: 0.5}), source}));
    """
    result = subprocess.run(
        [shutil.which("node"), "-", str(FORMAT)], input=script, text=True,
        capture_output=True, check=True, timeout=10,
    )
    payload = json.loads(result.stdout)
    values = [item["text"] for item in payload["outputs"]]
    assert values == [
        "0%", "100%", "12.5%", "1.00e-5%", "-4.5 percentage points", "4.5%",
        "0.001 ratio", "1 log2", "12,345 reads", "12,300 bp", "-0.0025 log2/GC",
        "unavailable (no eligible calls)", "unavailable", "unavailable",
        "0 (unit not declared)", "false (unit not declared)",
        "9.99e-4 ratio", "1.00e-3 ratio", "0.001 ratio", "9,007,199,254,740,991 reads",
        "9,010,000,000,000,000 reads", "2.5 reads", "1.00e+310%",
    ]
    assert payload["outputs"][4]["displayUnit"] == "percentage points"
    assert payload["outputs"][11]["available"] is False
    assert payload["delta"]["text"] == "-4.5 percentage points"
    assert payload["fragmentDelta"]["text"] == "-4.5 percentage points"
    assert payload["undeclared"]["text"] == "0.5 (unit not declared)"
    assert payload["categoricalFalse"]["text"] == "false"
    assert payload["categoricalText"]["text"] == "none"
    assert payload["categoricalNumber"]["text"] == "0.5 (unit not declared)"
    assert payload["source"]["value"] == -0.045 and payload["source"]["unit"] == "fraction"
