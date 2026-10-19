// Human-readable metric values. Raw values and units remain in the API payload.
"use strict";

(function () {
  const COUNT_UNITS = new Set([
    "arms", "bins", "bytes", "calls", "estimated reads", "fragments", "genes",
    "markers", "profiles", "reads", "records", "segments", "sites", "taxa",
  ]);
  const FRACTION_DIFFERENCES = new Set([
    "pmd-score/pmd_delta",
    "frag-x-meth/short_fragment_hypomethylation_delta",
  ]);
  const numberFormat = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 3 });
  const integerFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

  function numberText(value, count) {
    if (value === 0) return "0";
    if (count && Number.isSafeInteger(value)) return integerFormat.format(value);
    if (Math.abs(value) < 0.001) return value.toExponential(2);
    return numberFormat.format(value);
  }

  function displayedNumberText(value, fraction, count) {
    if (!fraction) return numberText(value, count);
    const percent = value * 100;
    if (Number.isFinite(percent)) return numberText(percent, false);
    // A finite but malformed out-of-range fraction can overflow the scale
    // conversion. Keep its value visible without emitting Infinity.
    const [coefficient, exponent] = value.toExponential(2).split("e");
    const shifted = Number(exponent) + 2;
    return `${coefficient}e${shifted >= 0 ? "+" : ""}${shifted}`;
  }

  function formatMetric(value, unit, options = {}) {
    const role = options.role || "value";
    const difference = role === "difference";
    const isFraction = unit === "fraction";
    const declaredUnit = typeof unit === "string" && unit.length > 0;
    const missingUnit = options.missingUnit !== false;
    const displayUnit = options.unitDisplay === false ? null
      : isFraction ? (difference ? "percentage points" : "%")
        : declaredUnit ? unit : missingUnit ? "unit not declared" : null;
    const available = value !== null && value !== undefined &&
      (typeof value !== "number" || Number.isFinite(value));
    const reason = typeof options.reason === "string" && options.reason
      ? options.reason.replace(/_/g, " ") : null;
    const displayValue = !available ? "unavailable"
      : typeof value === "number"
        ? displayedNumberText(value, isFraction,
          options.valueKind === "count" || COUNT_UNITS.has(unit))
        : String(value);
    const text = !available
      ? `unavailable${reason ? ` (${reason})` : ""}`
      : displayUnit === "unit not declared" ? `${displayValue} (unit not declared)`
        : displayUnit ? `${displayValue}${displayUnit === "%" ? "" : " "}${displayUnit}`
          : displayValue;
    return { text, displayValue, displayUnit, available };
  }

  function formatResultMetric(metric, options = {}) {
    const algorithmId = options.algorithmId || metric.algorithm_id;
    const role = options.role || (FRACTION_DIFFERENCES.has(`${algorithmId}/${metric.metric_id}`)
      ? "difference" : "value");
    const valueKind = options.valueKind || metric.value_kind || metric.entry?.value_kind || null;
    const nonnumericCategory = valueKind === "categorical" &&
      (typeof metric.value === "boolean" || typeof metric.value === "string");
    return formatMetric(metric.value, metric.unit, {
      ...options, role, reason: options.reason ?? metric.reason,
      valueKind, missingUnit: options.missingUnit ?? !nonnumericCategory,
    });
  }

  globalThis.TracebackLabFormat = { formatMetric, formatResultMetric };
})();
