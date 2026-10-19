/* Pure, bounded ResearchReportV1 projection. No DOM, fetch, or artifact access. */
(() => {
  "use strict";

  const MAX_ROWS = 5000;
  const MAX_BYTES = 5 * 1024 * 1024;
  const encoder = new TextEncoder();
  const built = new WeakSet();
  const sha = /^[0-9a-f]{64}$/;
  const id = /^[a-z0-9-]{1,40}$/;
  const metricId = /^[a-z0-9_]{1,64}$/;
  const version = /^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}$/;
  const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  const instant = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z$/;
  const tool = /^[A-Za-z0-9_.:-]{1,120}$/;
  const toolVersion = /^[A-Za-z0-9_.+:-]{1,40}$/;
  const reference = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const attempt = /^a-\d{8}T\d{12}-[0-9a-f]{8}$/;
  const units = new Set([
    "end-count difference", "fraction", "arms", "log2", "log2/GC", "phred", "reads",
    "copies", "reads per million", "ratio", "bp", "records", "x", "index", "fragments",
    "years", "z-score", "correlation", "relative", "sites", "bins", "segments", "genes",
    "profiles", "calls", "markers", "bytes", "estimated reads", "estimated reads/million",
    "taxa", "reads / 1M unmapped primaries",
  ]);
  const displayUnits = new Set([...units, "%", "percentage points", "unit not declared"]);
  const states = new Set([
    "not_interpretable", "no_reference", "in_range", "above_typical", "markedly_above",
    "below_typical", "markedly_below",
  ]);
  const reasons = new Set([
    "depth", "derived_input", "disk", "missing_toolchain", "missing_asset", "no_pon",
    "protocol_mismatch", "invalid_asset", "asset_unavailable", "missing_channel",
    "low_support", "missing_tags", "example_reference", "input_unavailable", "platform",
    "unsupported_input", "platform_unverified", "superseded_method", "derived_not_supported",
    "missing_input", "input_kind", "reference", "upstream", "unqualified_support",
    "missing_gpu", "model_not_ready",
  ]);
  const columns = [
    "schema_version", "generated_at", "research_use_only", "example", "sample_id", "source_id",
    "full_input_id", "run_utc", "interpretation_version", "cohort_version", "limitations",
    "algorithm_id", "metric_id", "label", "raw_value", "raw_unit", "display_value",
    "display_unit", "status", "skip_reason", "interpretation_state", "tool", "tool_version",
    "parameters_sha256", "attempt_id", "reference_id", "computed_at",
  ];

  function fail(code, message) {
    throw Object.assign(new Error(message), { code });
  }

  function validInstant(value, pattern = instant) {
    return typeof value === "string" && pattern.test(value) && !Number.isNaN(Date.parse(value));
  }

  function safeText(value, max = 160) {
    return typeof value === "string" && value.length > 0 && value.length <= max &&
      !/[\x00-\x1f\x7f/\\]/.test(value) &&
      !/(?:password|secret|token|api[_ -]?key|bearer|AKIA|sk-[A-Za-z0-9]{8}|[ACGTN]{30})/i.test(value);
  }

  function validProvenance(value, sample) {
    return value && typeof value === "object" && tool.test(value.tool) &&
      toolVersion.test(value.tool_version) && sha.test(value.parameters_sha256) &&
      value.source_id === sample.source_id && value.input_id === sample.full_input_id &&
      reference.test(value.reference_id) && validInstant(value.timestamp_utc, utc) &&
      (value.attempt_id == null || attempt.test(value.attempt_id));
  }

  function scalar(value, algorithmId, metricIdValue) {
    if (value == null) return { valid: true, value: null };
    if (typeof value === "number") return { valid: true, value: Number.isFinite(value) ? value : null,
      nonfinite: !Number.isFinite(value) };
    if (typeof value === "boolean") return { valid: true, value };
    if (typeof value === "string") {
      const allowed = algorithmId === "sex-check" && metricIdValue === "call"
        ? ["male", "female", "indeterminate"]
        : algorithmId === "copy-number" && metricIdValue === "pon_policy"
          ? ["none", "lab_loo_v1"] : [];
      return { valid: allowed.includes(value), value };
    }
    return { valid: false };
  }

  function sizeCheck(value) {
    if (encoder.encode(value).length > MAX_BYTES) fail("report_too_large", "The report exceeds the 5 MiB export limit.");
    return value;
  }

  function project(sample, generatedAt, formatResultMetric) {
    if (!sample || sample.found !== true || sample.state ||
        typeof sample.id12 !== "string" || !/^[0-9a-f]{12,64}$/.test(sample.id12) ||
        !sha.test(sample.source_id) || !sample.source_id.startsWith(sample.id12) ||
        !sha.test(sample.full_input_id) || typeof sample.example !== "boolean" ||
        !version.test(sample.interpretation_version) ||
        !(sample.cohort_version == null || version.test(sample.cohort_version)) ||
        !(sample.run_utc === "" || validInstant(sample.run_utc)) ||
        !validInstant(generatedAt) || !Array.isArray(sample.catalog) ||
        !Array.isArray(sample.algorithms) || typeof formatResultMetric !== "function") {
      fail("invalid_report_identity", "The selected sample cannot be prepared as a report.");
    }
    const limitations = [
      "Research use only; not for diagnosis or clinical decisions.",
      "Only visible registered scalar metrics are included; hidden processing steps, arrays, artifacts, and taxon detail are excluded.",
      "Stable input and provenance identifiers may be linkable to other local records.",
    ];
    const note = (message) => { if (!limitations.includes(message)) limitations.push(message); };
    const visible = new Set(sample.catalog.filter((row) => row && id.test(row.algorithm_id))
      .map((row) => row.algorithm_id));
    const metrics = [];
    let scanned = 0;
    for (const algorithm of sample.algorithms) {
      if (!algorithm || !visible.has(algorithm.algorithm_id) || algorithm.hidden === true) continue;
      const algorithmId = algorithm.algorithm_id;
      const status = algorithm.status;
      const reason = status === "skipped" && reasons.has(algorithm.skip_reason) ? algorithm.skip_reason : null;
      if (status === "skipped" || status === "failed") {
        note(`Method ${algorithmId}: ${status}${reason ? ` (${reason})` : ""}; no scalar rows.`);
        continue;
      }
      if (status !== "ok") { note(`Method ${algorithmId}: no eligible scalar rows.`); continue; }
      if (!Array.isArray(algorithm.metrics)) { note(`Method ${algorithmId}: no eligible scalar rows.`); continue; }
      for (const metric of algorithm.metrics) {
        if (++scanned > 100000) fail("report_too_large", "The report exceeds the export processing limit.");
        if (!metric || !metricId.test(metric.metric_id)) {
          note("Some metrics were omitted because their identifiers or values were unsupported.");
          continue;
        }
        const raw = scalar(metric.value, algorithmId, metric.metric_id);
        if (!raw.valid) {
          note("Some metrics were omitted because their identifiers or values were unsupported.");
          continue;
        }
        if (!validProvenance(algorithm.provenance, sample)) {
          note("Some metrics were omitted because their provenance was unavailable or invalid.");
          continue;
        }
        if (!(metric.unit == null || units.has(metric.unit))) {
          note("Some metrics were omitted because their units were unsupported.");
          continue;
        }
        if (raw.nonfinite) note("Some nonfinite numeric values are unavailable in this report.");
        const label = safeText(metric.label) ? metric.label : metric.metric_id;
        if (label !== metric.label) note("Some metric labels were unavailable; metric identifiers are shown instead.");
        const interpretationState = metric.state == null || states.has(metric.state) ? metric.state ?? null : null;
        if (metric.state != null && interpretationState == null) note("Some interpretation states were unavailable.");
        // Pass only vetted scalar fields; free-form reason, entry, and API diagnostics never reach the formatter.
        let formatted;
        try {
          formatted = formatResultMetric({ algorithm_id: algorithmId, metric_id: metric.metric_id,
            value: raw.value, unit: metric.unit ?? null,
            value_kind: metric.value_kind === "count" || metric.value_kind === "categorical"
              ? metric.value_kind : null }, { algorithmId, reason: null });
        } catch (_) {
          note("Some metrics were omitted because their display values were unavailable.");
          continue;
        }
        if (!formatted || !safeText(formatted.displayValue, 120) ||
            !(formatted.displayUnit == null || displayUnits.has(formatted.displayUnit))) {
          note("Some metrics were omitted because their display values were unavailable.");
          continue;
        }
        if (metrics.length >= MAX_ROWS) fail("report_too_many_rows", "The report exceeds the 5,000 row export limit.");
        metrics.push({
          algorithm_id: algorithmId, metric_id: metric.metric_id, label,
          raw_value: raw.value, raw_unit: metric.unit ?? null,
          display_value: formatted.displayValue, display_unit: formatted.displayUnit ?? null,
          status, skip_reason: reason, interpretation_state: interpretationState,
          tool: algorithm.provenance.tool, tool_version: algorithm.provenance.tool_version,
          parameters_sha256: algorithm.provenance.parameters_sha256,
          attempt_id: algorithm.provenance.attempt_id ?? null,
          reference_id: algorithm.provenance.reference_id,
          computed_at: algorithm.provenance.timestamp_utc,
        });
      }
    }
    if (!metrics.length) fail("no_report_rows", "No eligible scalar metrics are available for this sample.");
    const report = {
      schema_version: "1", generated_at: generatedAt, research_use_only: true,
      example: sample.example, sample_id: sample.id12, source_id: sample.source_id,
      full_input_id: sample.full_input_id, run_utc: sample.run_utc || null,
      interpretation_version: sample.interpretation_version,
      cohort_version: sample.cohort_version ?? null,
      limitations: Object.freeze(limitations), metrics: Object.freeze(metrics.map((row) => Object.freeze(row))),
    };
    sizeCheck(JSON.stringify(report));
    Object.freeze(report);
    built.add(report);
    return report;
  }

  function json(report) {
    if (!built.has(report)) fail("invalid_report_model", "Prepare a report before downloading it.");
    return sizeCheck(`${JSON.stringify(report, null, 2)}\n`);
  }

  function csv(report) {
    if (!built.has(report)) fail("invalid_report_model", "Prepare a report before downloading it.");
    const text = (value) => {
      if (value == null) return "";
      if (typeof value === "boolean" || typeof value === "number") return String(value);
      let result = String(value);
      if (/^[\t\r\n]/.test(result) || /^[\s]*[=+\-@]/.test(result)) result = `'${result}`;
      return `"${result.replace(/"/g, '""')}"`;
    };
    const identity = Object.fromEntries(columns.slice(0, 10).map((key) => [key, report[key]]));
    const lines = [columns.join(",")];
    for (const row of report.metrics) {
      const values = { ...identity, limitations: report.limitations.join(" | "), ...row };
      lines.push(columns.map((key) => text(values[key])).join(","));
    }
    return sizeCheck(`${lines.join("\r\n")}\r\n`);
  }

  function filename(report, extension) {
    if (!built.has(report) || !["json", "csv"].includes(extension))
      fail("invalid_report_model", "Prepare a report before downloading it.");
    const stamp = report.generated_at.slice(0, 19).replace(/[-:]/g, "");
    return `traceback-research-${report.sample_id}-${stamp}Z.${extension}`;
  }

  globalThis.TracebackLabReport = { project, json, csv, filename };
})();
