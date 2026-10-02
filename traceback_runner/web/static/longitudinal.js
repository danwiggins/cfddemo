// E12 longitudinal view: renders only the D08 public projection and the closed
// E12 route responses.  It never infers a connecting line: the chart draws only
// LongitudinalSegment entries, and x positions use signed-seconds offsets.
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.TracebackLongitudinal = api;
    api.install(root);
  }
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const STATES = [
    "loading", "empty", "error", "success", "partial",
    "stale", "revoked", "permission-denied", "slow-stage",
  ];
  const SLOW_STAGE_MS = 2000;
  // The SVG XML namespace name.  It is an identifier, never fetched; it is
  // assembled so the packaged-asset scan for external references stays exact.
  const SVG_NS = ["http", "://www.w3.org/2000/svg"].join("");
  const COMPARABLE = new Set(["equivalent", "qualified_compatible"]);
  const ACTIONS = {
    equivalent: "use_direct_comparison",
    qualified_compatible: "use_qualified_comparison",
    requires_reanalysis: "request_reanalysis",
    registered_bridge: "review_registered_bridge",
    incompatible: "start_separate_series",
    unknown: "resolve_unknown_inputs",
  };
  const RENDERING = {
    equivalent: "Eligible for D07 comparison after every other gate passes.",
    qualified_compatible: "Eligible for D07 comparison after every other gate passes; qualified evidence applies.",
    requires_reanalysis: "Separate source series; no D07 numeric comparison or segment.",
    registered_bridge: "Separate source series; the bridge is shown for review and never executed.",
    incompatible: "Separate source series; exact mismatches are listed.",
    unknown: "No comparison; exact unknown dimensions are listed.",
    anchor: "Pinned anchor: the reference for every comparison.",
    not_evaluated: "Not decided by the D03 series; no comparison.",
  };
  const PROBLEMS = {
    permission_denied: ["Permission denied.", "Ask the operator for a current reader launch link."],
    invalid_request: ["The selection does not match current registered authority.", "Reselect from the current lists."],
    authority_stale: ["Authority changed or is not current.", "Retry; if it persists, reselect the cohort version."],
    read_conflict: ["Authority moved while reading.", "Retry the read."],
    trust_revoked: ["A trust authority could not be verified.", "Review the trust authority with the operator."],
    integrity_failure: ["A local store failed an integrity check.", "Verify local store integrity with the operator."],
    storage_failure: ["Local storage is unavailable.", "Check local storage, then retry."],
    save_unavailable: ["Save is unavailable.", "The saved-comparison registry is absent, unhealthy or full."],
  };
  const REMEDIATIONS = {
    correct_request: "Correct the selection.",
    reduce_cohort_to_bound: "The cohort exceeds 1,000 members.",
    obtain_current_reader_grant: "Obtain a current reader grant.",
    reselect_cohort_version: "Reselect the cohort version.",
    reselect_anchor: "Reselect the approved anchor.",
    reselect_policy: "Reselect the policy.",
    refresh_source_registration: "Refresh the source registration.",
    refresh_decision_registration: "Refresh the D03 decision registration.",
    retry_read: "Retry the read.",
    verify_store_integrity: "Verify local store integrity.",
    check_local_storage: "Check local storage.",
    review_trust_authority: "Review the trust authority.",
    registry_absent: "No saved-comparison registry is installed.",
    registry_unhealthy: "The saved-comparison registry is unhealthy.",
    registry_full: "The saved-comparison registry is at its 1,000-object bound.",
  };
  const PREREQUISITES = {
    e08_cell_origin_artifact_binding: "E08 cell-origin artifact binding",
    e09_cna_artifact_binding: "E09 CNA artifact binding",
  };
  const OPERATOR_ENTERED = "operator-entered, unverified";

  // --- pure helpers (unit-tested under node) ---------------------------------------

  const text = (value) => (value === null || value === undefined ? "none" : String(value));
  const words = (value) => text(value).replace(/_/g, " ");

  const problemFor = (code, remediation) => {
    const known = PROBLEMS[code] || ["The request failed.", "Retry the request."];
    const fix = remediation && REMEDIATIONS[remediation] ? REMEDIATIONS[remediation] : known[1];
    return { code: code || "unknown_error", problem: known[0], remediation: fix };
  };

  const comparableDraws = (rows) =>
    rows.filter((row) => row.lineage_role === "biological_draw"
      && (row.comparison_state === "anchor_reference" || row.comparison_state === "available")).length;

  const classifyWorkspace = (workspace) => {
    const rows = workspace.rows || [];
    if (!rows.length || comparableDraws(rows) < 2) return "empty";
    if (rows.some((row) => row.withheld_reason === "result_key_revoked")) return "revoked";
    const partial = rows.some((row) => row.record_availability !== "available"
      || (row.compatibility_state !== "anchor" && !COMPARABLE.has(row.compatibility_state))
      || row.comparison_state === "suppressed"
      || row.value_state !== "projected");
    return partial ? "partial" : "success";
  };

  const emptyReason = (workspace) => {
    const rows = workspace.rows || [];
    if (!rows.length) return "No verified source records match the active filters.";
    return "Fewer than two comparable biological draws exist for this anchor, so no comparison series is drawn.";
  };

  // Chart geometry: x from signed-seconds offsets (unequal intervals stay unequal);
  // lines only from explicit LongitudinalSegment entries, one SVG path per series.
  const chartModel = (workspace, dims) => {
    const size = Object.assign({ width: 640, height: 320, margin: 48 }, dims || {});
    const rows = workspace.rows || [];
    const byOrdinal = new Map(rows.map((row) => [row.row_ordinal, row]));
    const anchorRow = rows.find((row) => row.comparison_state === "anchor_reference");
    const compared = rows.filter((row) => row.comparison);
    if (!anchorRow || !compared.length) return null;
    const anchorComparison = compared[0].comparison;
    const values = new Map();
    values.set(anchorRow.row_ordinal, {
      y: anchorComparison.anchor_value,
      lower: anchorComparison.anchor_uncertainty_lower,
      upper: anchorComparison.anchor_uncertainty_upper,
    });
    compared.forEach((row) => values.set(row.row_ordinal, {
      y: row.comparison.member_value,
      lower: row.comparison.member_uncertainty_lower,
      upper: row.comparison.member_uncertainty_upper,
    }));
    const plotted = [...values.keys()].map((ordinal) => byOrdinal.get(ordinal));
    const offsets = plotted.map((row) => row.offset_seconds);
    const ys = [...values.values()].flatMap((item) => [item.lower, item.upper, item.y]);
    const xMin = Math.min(...offsets);
    const xMax = Math.max(...offsets);
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    const innerW = size.width - 2 * size.margin;
    const innerH = size.height - 2 * size.margin;
    const xOf = (offset) => (xMax === xMin ? size.margin + innerW / 2
      : size.margin + ((offset - xMin) / (xMax - xMin)) * innerW);
    const yOf = (value) => (yMax === yMin ? size.margin + innerH / 2
      : size.margin + (1 - (value - yMin) / (yMax - yMin)) * innerH);
    const points = plotted.map((row) => {
      const value = values.get(row.row_ordinal);
      return {
        row_ordinal: row.row_ordinal,
        offset_seconds: row.offset_seconds,
        x: xOf(row.offset_seconds),
        y: yOf(value.y),
        yLower: yOf(value.lower),
        yUpper: yOf(value.upper),
        value: value.y,
      };
    });
    const pointOf = new Map(points.map((point) => [point.row_ordinal, point]));
    const series = [];
    const segments = (workspace.segments || []).slice().sort(
      (a, b) => a.from_timepoint_ordinal - b.from_timepoint_ordinal);
    segments.forEach((segment) => {
      const from = pointOf.get(segment.from_row_ordinal);
      const to = pointOf.get(segment.to_row_ordinal);
      if (!from || !to) return;
      const last = series[series.length - 1];
      if (last && last.rows[last.rows.length - 1] === segment.from_row_ordinal) {
        last.rows.push(segment.to_row_ordinal);
      } else {
        series.push({ rows: [segment.from_row_ordinal, segment.to_row_ordinal] });
      }
    });
    series.forEach((item, index) => {
      item.id = index + 1;
      item.d = item.rows.map((ordinal, position) => {
        const point = pointOf.get(ordinal);
        return `${position ? "L" : "M"}${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
      }).join(" ");
    });
    const connected = new Set(series.flatMap((item) => item.rows));
    return {
      width: size.width,
      height: size.height,
      points,
      series,
      unconnected: points.filter((point) => !connected.has(point.row_ordinal)).map((p) => p.row_ordinal),
    };
  };

  const drawerMode = (widthPx) => (widthPx >= 1024 ? "beside" : widthPx >= 672 ? "overlay" : "sheet");

  const isFocusable = (element) => {
    if (!element || element.hidden || element.disabled) return false;
    const tag = String(element.tagName || "").toLowerCase();
    if (["button", "select", "input", "textarea"].includes(tag)) return true;
    if (tag === "a" && element.getAttribute && element.getAttribute("href")) return true;
    return Boolean(element.getAttribute && element.getAttribute("tabindex") === "0");
  };

  const focusables = (container) => {
    const found = [];
    const walk = (element) => {
      if (!element || element.hidden) return;
      if (element !== container && isFocusable(element)) found.push(element);
      Array.from(element.children || []).forEach(walk);
    };
    walk(container);
    return found;
  };

  // Mobile sheet focus containment: Tab from the last control returns to the first,
  // Shift+Tab from the first goes to the last.  Returns the element to focus or null.
  const containFocus = (container, active, backwards) => {
    const items = focusables(container);
    if (!items.length) return null;
    const index = items.indexOf(active);
    if (index === -1) return items[0];
    if (backwards && index === 0) return items[items.length - 1];
    if (!backwards && index === items.length - 1) return items[0];
    return null;
  };

  const valueText = (row) => {
    if (row.value_state === "projected") {
      return row.values.map((value) => {
        const bounds = `bin ${value.bin_index} [${value.lower_inclusive}, ${value.upper_exclusive === null ? "unbounded" : value.upper_exclusive})`;
        const exact = value.count !== null
          ? `count ${value.count}`
          : `fraction ${value.fraction_numerator}/${value.fraction_denominator}`;
        return `${words(value.statistic)} ${exact} (${value.statistic_unit}; ${bounds})`;
      }).join("; ");
    }
    if (row.value_state === "family_prerequisite_missing") {
      return `unavailable: prerequisite ${PREREQUISITES[row.missing_prerequisite] || words(row.missing_prerequisite)}`;
    }
    if (row.value_state === "projection_rejected") return `rejected: ${words(row.value_rejection)}`;
    return `unavailable: ${words(row.value_state)}`;
  };

  const comparisonText = (row) => {
    if (row.comparison_state === "anchor_reference") return "anchor reference";
    if (row.comparison_state === "available" && row.comparison) {
      const c = row.comparison;
      return `available: member ${c.member_value} vs anchor ${c.anchor_value}; difference ${c.delta} (member interval ${c.member_uncertainty_lower} to ${c.member_uncertainty_upper}; denominators ${c.member_denominator_count} and ${c.anchor_denominator_count}); descriptive only`;
    }
    if (row.comparison_state === "suppressed_stale_authority") return "suppressed: saved comparison is stale";
    return `suppressed: ${(row.suppression_reasons || []).map(words).join(", ") || "not eligible"}`;
  };

  // --- DOM rendering ------------------------------------------------------------------

  const el = (doc, tag, value, attrs) => {
    const node = doc.createElement(tag);
    if (value !== undefined && value !== null) node.textContent = String(value);
    Object.entries(attrs || {}).forEach(([name, attr]) => node.setAttribute(name, String(attr)));
    return node;
  };
  const svg = (doc, tag, attrs) => {
    const node = doc.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([name, attr]) => node.setAttribute(name, String(attr)));
    return node;
  };
  const pairs = (doc, list, entries) => {
    list.replaceChildren();
    entries.forEach(([term, value]) => list.append(el(doc, "dt", term), el(doc, "dd", value)));
  };

  const renderIdentity = (doc, ui, workspace, context) => {
    const a = workspace.authority;
    pairs(doc, ui.identity, [
      ["Cohort selector and exact version", `${a.cohort_selector_id} version ${a.cohort_version}`],
      ["Cohort manifest digest", a.cohort_manifest_sha256],
      ["Authority state", context.authorityLabel],
      ["Anchor policy approval", `${a.anchor_policy_selector_id} version ${a.anchor_policy_version}`],
      ["Approved anchor", `${a.anchor_selector_id} (row ${a.anchor_row_ordinal})`],
      ["Projection policy", `${a.projection_selector_id} version ${a.projection_policy_version} (${words(a.projection_family)})`],
      ["D03 series", words(a.d03_series_state)],
      ["Replay digest", workspace.replay_sha256],
      ["Filter digest", workspace.filters_sha256],
      ["Release state", "Release, export and diagnostic interpretation are not authorized"],
    ]);
  };

  const renderOutcome = (doc, ui, workspace) => {
    ui.outcome.replaceChildren();
    const rows = workspace.rows || [];
    const outcomes = [...new Set(rows.map((row) => row.compatibility_state))];
    const list = el(doc, "ul");
    outcomes.forEach((state) => {
      const count = rows.filter((row) => row.compatibility_state === state).length;
      const action = ACTIONS[state] ? `; permitted next action: ${words(ACTIONS[state])}` : "";
      list.append(el(doc, "li", `${words(state)} (${count} rows)${action}. ${RENDERING[state] || ""}`,
        { "data-outcome": state }));
    });
    ui.outcome.append(list, el(doc, "p", workspace.limitation_statement, { class: "help" }));
  };

  const renderDenominators = (doc, ui, workspace) => {
    const p = workspace.population;
    ui.strip.replaceChildren(
      el(doc, "li", `Declared: ${p.declared_members} members, ${p.declared_denominator_units} units`),
      el(doc, "li", `Included: ${p.included_members} members, ${p.included_denominator_units} units`),
      el(doc, "li", `Excluded: ${p.excluded_members} members, ${p.excluded_denominator_units} units`),
      el(doc, "li", `Unavailable: ${p.unavailable_members} members, ${p.unavailable_denominator_units} units`),
      el(doc, "li", `Summary state: ${words(p.state)}. D09 counts are population context, not a comparison gate; filters never change them.`),
    );
  };

  const renderRows = (doc, ui, rows, onDetails) => {
    ui.rows.replaceChildren();
    rows.forEach((row) => {
      const tr = el(doc, "tr", null, {
        "data-row": row.row_ordinal,
        "data-outcome": row.compatibility_state,
        "data-record": row.record_availability,
      });
      const header = el(doc, "th", `${row.row_ordinal} (${row.source_alias})`, { scope: "row" });
      tr.append(header);
      const reasons = [
        ...(row.compatibility_reasons || []),
        ...(row.mismatch_dimensions || []).map((item) => `mismatch ${item}`),
        ...(row.unknown_dimensions || []).map((item) => `unknown ${item}`),
      ];
      const record = row.record_availability === "withheld"
        ? `withheld (${words(row.withheld_reason)})`
        : row.record_availability;
      const history = row.affected_comparison_warning
        ? `${words(row.history_state)}; affected comparison warning`
        : words(row.history_state);
      [
        `timepoint ${row.timepoint_ordinal}`,
        String(row.offset_seconds),
        words(row.lineage_role),
        row.denominator_contributor ? "biological unit" : "not a denominator unit",
        record,
        history,
        words(row.compatibility_state) + (row.bridge_reference_count ? `; ${row.bridge_reference_count} bridge references, not executed` : ""),
        reasons.length ? reasons.map(words).join(", ") : "none",
        row.next_action ? words(row.next_action) : (row.compatibility_state === "anchor" ? "anchor reference" : "none"),
        row.value_state ? valueText(row) : "not shown",
        comparisonText(row),
      ].forEach((value) => tr.append(el(doc, "td", value)));
      const cell = el(doc, "td");
      const button = el(doc, "button", `Details for row ${row.row_ordinal}`, { type: "button", class: "lg-details" });
      if (onDetails) button.addEventListener("click", () => onDetails(row, button));
      cell.append(button);
      tr.append(cell);
      ui.rows.append(tr);
    });
  };

  const renderChart = (doc, ui, workspace) => {
    ui.chart.replaceChildren();
    const model = chartModel(workspace);
    if (!model) {
      ui.chart.append(el(doc, "p", "No chart: no authorized D07 comparison is available. The table lists every source record.", { class: "help" }));
      return model;
    }
    const root = svg(doc, "svg", {
      viewBox: `0 0 ${model.width} ${model.height}`,
      role: "img",
      "aria-labelledby": "lg-svg-title lg-svg-desc",
      class: "lg-svg",
    });
    const title = svg(doc, "title", { id: "lg-svg-title" });
    title.textContent = "Anchor-relative D07 comparison values by biological collection offset";
    const desc = svg(doc, "desc", { id: "lg-svg-desc" });
    desc.textContent = `${model.series.length} authorized series and ${model.unconnected.length} unconnected points. X positions follow signed-seconds offsets. Lines are drawn only for registered segments.`;
    root.append(title, desc);
    root.append(svg(doc, "line", { x1: 48, y1: model.height - 48, x2: model.width - 48, y2: model.height - 48, class: "lg-axis" }));
    model.series.forEach((item) => {
      root.append(svg(doc, "path", { d: item.d, class: "lg-series", "data-series": item.id, fill: "none" }));
    });
    model.points.forEach((point) => {
      root.append(svg(doc, "line", { x1: point.x, x2: point.x, y1: point.yLower, y2: point.yUpper, class: "lg-interval" }));
      const marker = model.unconnected.includes(point.row_ordinal)
        ? svg(doc, "rect", { x: point.x - 5, y: point.y - 5, width: 10, height: 10, class: "lg-point lg-unconnected", "data-row": point.row_ordinal })
        : svg(doc, "circle", { cx: point.x, cy: point.y, r: 5, class: "lg-point", "data-row": point.row_ordinal });
      root.append(marker);
      const label = svg(doc, "text", { x: point.x + 8, y: point.y - 8, class: "lg-label" });
      label.textContent = `row ${point.row_ordinal}`;
      root.append(label);
    });
    const legend = el(doc, "ul", null, { class: "lg-legend" });
    model.series.forEach((item) => legend.append(el(doc, "li", `Series ${item.id}: rows ${item.rows.join(", ")} (solid line, registered segments only)`)));
    if (model.unconnected.length) {
      legend.append(el(doc, "li", `Unconnected (square markers): rows ${model.unconnected.join(", ")}`));
    }
    ui.chart.append(root, legend);
    return model;
  };

  const renderCovariates = (doc, ui, workspace) => {
    const c = workspace.covariate_context;
    ui.covariates.replaceChildren();
    const list = el(doc, "dl");
    pairs(doc, list, [
      ["Context state", words(c.state)],
      ["Classification", words(c.classification)],
      ["Limitation reasons", (c.reason_codes || []).map(words).join(", ") || "none"],
      ["Included members", text(c.included_member_count)],
      ["Covariate tokens", OPERATOR_ENTERED],
      ["Values or eligibility changed", "no"],
      ["Biological attribution", "not allowed"],
    ]);
    ui.covariates.append(list);
    (c.groups || []).forEach((group) => {
      ui.covariates.append(el(doc, "p",
        `Group ${group.group_index + 1}: ${group.member_count} members; states ${(group.states || []).map(words).join(", ")}`,
        { class: "help" }));
    });
  };

  const renderDrawer = (doc, ui, row, detail) => {
    const list = el(doc, "dl");
    pairs(doc, list, [
      ["Row", `${row.row_ordinal} (${row.source_alias})`],
      ["Method", row.method_ref ? `${row.method_ref.method_id} ${row.method_ref.version}` : "none"],
      ["Catalog result digest", text(row.catalog_result_sha256)],
      ["Source digest", text(row.source_sha256)],
      ["Source replay digest", text(row.source_replay_sha256)],
      ["E06 denominator ledger", row.denominator_ledger_sha256 ? `${row.denominator_ledger_sha256} (${OPERATOR_ENTERED})` : "none"],
      ["Execution and information state", `${words(row.execution_state)}; ${words(row.information_state)}`],
      ["D03 decision digest", text(row.decision_sha256)],
      ["Mismatch dimensions", (row.mismatch_dimensions || []).map(words).join(", ") || "none"],
      ["Unknown dimensions", (row.unknown_dimensions || []).map(words).join(", ") || "none"],
      ["Bridge", row.bridge_reference_count ? `${row.bridge_reference_count} references; not executed` : "none"],
      ["Comparison", comparisonText(row)],
      ["Fresh revision", detail ? detail.replay_sha256 : "loading"],
    ]);
    ui.drawerBody.replaceChildren(list);
  };

  const renderVersionDiff = (doc, container, diff) => {
    const list = el(doc, "dl");
    pairs(doc, list, [
      ["Selected version", `${diff.selected_version} (${words(diff.kind)})`],
      ["Predecessor version", text(diff.predecessor_version)],
      ["Members added", String(diff.added_member_count)],
      ["Members removed", String(diff.removed_member_count)],
      ["Members unchanged", String(diff.unchanged_member_count)],
      ["Added member set digest", diff.added_member_set_sha256],
      ["Removed member set digest", diff.removed_member_set_sha256],
      ["Selected manifest digest", diff.selected_manifest_sha256],
      ["Predecessor manifest digest", text(diff.predecessor_manifest_sha256)],
      ["Changes", (diff.reasons || []).map(words).join(", ") || "none"],
      ["D03 policy change", words(diff.d03_policy_change)],
      ["Silent upgrade", "never"],
    ]);
    container.append(list);
  };

  const renderReopenDiff = (doc, container, diff) => {
    container.replaceChildren();
    const list = el(doc, "dl");
    pairs(doc, list, [
      ["Saved comparison", `${diff.saved_selector_id} version ${diff.comparison_version}`],
      ["Saved bytes digest", diff.object_sha256],
      ["Comparison state", diff.comparison_state === "current" ? "current" : "stale: authority differs from the saved comparison"],
      ["Registry authority", words(diff.registry_authority_state)],
      ["Changed dependencies", (diff.stale_dependencies || []).map(words).join(", ") || "none"],
      ["Changed commitments", (diff.changes || []).map(words).join(", ") || "none"],
      ["Saved cohort version", `${diff.saved_selection.cohort_selector_id} version ${diff.saved_selection.cohort_version}`],
      ["Saved measurement", `${words(diff.saved_selection.measurement.quantity_id)} (${diff.saved_selection.measurement.unit}); definition ${diff.saved_selection.measurement.measurement_definition_sha256}`],
      ["Saved anchor", `${diff.saved_selection.anchor_policy_selector_id} approval ${diff.saved_selection.anchor_policy_version}; candidate ${diff.saved_selection.approved_anchor_selector_id}`],
      ["Saved projection policy", `${diff.saved_selection.projection_policy_selector_id} version ${diff.saved_selection.projection_policy_version}`],
      ["Saved D09 policy", `${diff.saved_selection.d09_policy_selector_id} version ${diff.saved_selection.d09_policy_version}`],
      ["Saved replay digest", diff.saved_workspace_replay_sha256],
      ["Rebuilt replay digest", text(diff.rebuilt_workspace_replay_sha256)],
      ["Newer cohort version", diff.newer_cohort_version_available ? `available (version ${diff.latest_cohort_version}); not applied` : "none"],
      ["Rebuild", diff.rebuild_error ? `failed: ${words(diff.rebuild_error)}` : "rebuilt from current authority"],
      ["Saved bytes", "immutable; never rewritten or relabelled as current"],
    ]);
    container.append(list);
    if (diff.cohort_version_diff) renderVersionDiff(doc, container, diff.cohort_version_diff);
  };

  // A stale saved comparison shows only its immutable commitments: the saved
  // bytes hold no rows, and current rows must not be relabelled as saved ones.
  const renderHistorical = (doc, container, commitments) => {
    const list = el(doc, "dl");
    pairs(doc, list, Object.entries(commitments || {}).map(([name, value]) => [words(name), value]));
    container.replaceChildren(
      el(doc, "p", "Historical saved commitments (immutable). No values, comparisons or segments are shown for a stale comparison.", { class: "help" }),
      list);
  };

  const renderWorkspace = (doc, ui, workspace, context, onDetails) => {
    renderIdentity(doc, ui, workspace, context);
    renderOutcome(doc, ui, workspace);
    renderDenominators(doc, ui, workspace);
    renderRows(doc, ui, workspace.rows || [], onDetails);
    const chart = renderChart(doc, ui, workspace);
    renderCovariates(doc, ui, workspace);
    return chart;
  };

  // --- controller -----------------------------------------------------------------------

  const install = (win) => {
    const doc = win.document;
    const byId = (id) => doc.getElementById(id);
    const section = byId("longitudinal");
    if (!section) return;
    const ui = {
      status: byId("lg-status"),
      problem: byId("lg-problem"),
      problemText: byId("lg-problem-text"),
      problemFix: byId("lg-problem-fix"),
      retry: byId("lg-retry"),
      cohort: byId("lg-cohort"),
      scope: byId("lg-scope"),
      measurement: byId("lg-measurement"),
      anchorPolicy: byId("lg-anchor-policy"),
      anchor: byId("lg-anchor"),
      d09: byId("lg-d09"),
      journey: byId("lg-journey"),
      showDiff: byId("lg-show-diff"),
      showResults: byId("lg-show-results"),
      diff: byId("lg-diff"),
      diffBody: byId("lg-diff-body"),
      results: byId("lg-results"),
      identity: byId("lg-identity-body"),
      outcome: byId("lg-outcome-body"),
      strip: byId("lg-denominator-strip"),
      rows: byId("lg-rows"),
      chart: byId("lg-chart-body"),
      covariates: byId("lg-covariates-body"),
      save: byId("lg-save"),
      saveState: byId("lg-save-state"),
      receipt: byId("lg-receipt"),
      drawer: byId("lg-drawer"),
      drawerBody: byId("lg-drawer-body"),
      drawerClose: byId("lg-drawer-close"),
      savedList: byId("lg-saved-list"),
    };
    let csrf = null;
    let scopes = [];
    let catalog = null;
    let currentRequest = null;
    let diffShownFor = null;
    // The whole journey step to repeat on Retry (not just its fetch).
    let retryStep = null;
    let inflight = null;
    let ticker = null;
    let drawerOpener = null;
    let lastView = null;

    const setState = (state) => {
      section.dataset.state = state;
      section.setAttribute("aria-busy", state === "loading" || state === "slow-stage" ? "true" : "false");
    };
    const stopTicker = () => { if (ticker) win.clearInterval(ticker); ticker = null; };
    const showProblem = (code, remediation) => {
      const problem = problemFor(code, remediation);
      ui.problem.hidden = false;
      ui.problemText.textContent = `${problem.code}: ${problem.problem}`;
      ui.problemFix.textContent = problem.remediation;
      ui.retry.hidden = code === "permission_denied";
    };
    const clearProblem = () => { ui.problem.hidden = true; };
    const hideResults = () => { ui.results.hidden = true; ui.rows.replaceChildren(); ui.chart.replaceChildren(); };

    const call = async (method, path, body) => {
      if (inflight) inflight.abort();
      const controller = new win.AbortController();
      inflight = controller;
      const init = { method, credentials: "same-origin", signal: controller.signal, headers: {} };
      if (method === "POST") {
        init.headers["Content-Type"] = "application/json";
        init.headers["X-Traceback-CSRF"] = csrf;
        init.body = JSON.stringify(body);
      }
      const response = await win.fetch(path, init);
      let payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      if (inflight === controller) inflight = null;
      return { status: response.status, payload };
    };

    // One stage at a time: exact stage label and elapsed time, no percentage.
    const runStage = async (label, action) => {
      clearProblem();
      stopTicker();
      const started = Date.now();
      setState("loading");
      ui.status.textContent = `Loading: ${label} (0 s elapsed)`;
      ticker = win.setInterval(() => {
        const elapsed = Math.round((Date.now() - started) / 1000);
        if (Date.now() - started >= SLOW_STAGE_MS) {
          setState("slow-stage");
          ui.status.textContent = `Slow stage: ${label} is still running (${elapsed} s elapsed). You can leave this page; returning fetches a fresh revision.`;
        } else {
          ui.status.textContent = `Loading: ${label} (${elapsed} s elapsed)`;
        }
      }, 1000);
      try {
        const result = await action();
        stopTicker();
        if (result && result.status === 403) {
          hideResults();
          setState("permission-denied");
          ui.status.textContent = "Permission denied.";
          showProblem("permission_denied");
          return null;
        }
        if (!result || result.status !== 200 || !result.payload) {
          hideResults();
          setState("error");
          const error = result && result.payload && result.payload.error ? result.payload.error : {};
          ui.status.textContent = `Error: ${label} failed.`;
          showProblem(error.code, error.remediation);
          return null;
        }
        return result.payload;
      } catch (error) {
        stopTicker();
        if (error && error.name === "AbortError") return null;
        hideResults();
        setState("error");
        ui.status.textContent = `Error: ${label} failed locally.`;
        showProblem("storage_failure");
        return null;
      }
    };

    const option = (value, label) => {
      const node = el(doc, "option", label);
      node.value = value;
      return node;
    };
    const fill = (select, items, placeholder) => {
      select.replaceChildren(option("", placeholder), ...items);
    };
    const scopeParams = () => {
      const chosen = catalog && catalog.measurement_options[Number(ui.measurement.value)];
      const scope = chosen ? chosen.measurement : scopes[Number(ui.scope.value || 0)];
      return { family: scope.family, quantity_id: scope.quantity_id, unit: scope.unit };
    };

    const buildRequest = () => {
      const [cohortId, cohortVersion] = ui.cohort.value.split("|");
      const measurement = catalog && catalog.measurement_options[Number(ui.measurement.value)];
      const [anchorPolicyId, anchorPolicyVersion] = ui.anchorPolicy.value.split("|");
      const [d09Id, d09Version] = ui.d09.value.split("|");
      const candidates = catalog && catalog.anchor_candidates;
      if (!cohortId || !measurement || !anchorPolicyId || !ui.anchor.value || !d09Id || !candidates) return null;
      const checked = (name) => Array.from(ui.journey.querySelectorAll(`input[name="${name}"]:checked`)).map((item) => item.value);
      return {
        cohort_selector_id: cohortId,
        cohort_version: Number(cohortVersion),
        anchor_policy_selector_id: anchorPolicyId,
        anchor_policy_version: Number(anchorPolicyVersion),
        anchor_selector_id: ui.anchor.value,
        anchor_candidate_page_sha256: candidates.candidate_page_sha256,
        projection_policy_selector_id: measurement.projection_policy_selector_id,
        projection_policy_version: measurement.projection_policy_version,
        d09_policy_selector_id: d09Id,
        d09_policy_version: Number(d09Version),
        measurement: measurement.measurement,
        filters: {
          timepoint_ordinals: [],
          lineage_roles: checked("lineage_roles"),
          record_availability: checked("record_availability"),
          compatibility_states: [],
        },
      };
    };
    const requestKey = (request) => (request ? JSON.stringify(request) : null);
    const refreshResultsButton = () => {
      const request = buildRequest();
      ui.showResults.disabled = !request || requestKey(request) !== diffShownFor;
    };

    const loadCohorts = async () => {
      retryStep = loadCohorts;
      const payload = await runStage("cohort selectors", () => call("GET", "/api/v1/longitudinal/selectors"));
      if (!payload) return;
      scopes = payload.measurement_scopes;
      ui.scope.replaceChildren(...scopes.map((item, index) => option(String(index),
        `${words(item.quantity_id)} (${item.unit}; ${words(item.family)})`)));
      ui.scope.value = "0";
      fill(ui.cohort, payload.cohorts.map((item) => option(
        `${item.cohort_selector_id}|${item.cohort_version}`,
        `${item.cohort_selector_id.slice(0, 24)} version ${item.cohort_version} (${words(item.authority_state)}; ${item.member_count} members)`,
      )), "Choose a cohort version");
      setState("empty");
      ui.status.textContent = payload.cohorts.length
        ? "Choose a cohort version, measurement, anchor and denominator policy."
        : "No registered cohort versions are available.";
      loadSaved();
    };

    const loadCohortOptions = async (anchorParams) => {
      retryStep = () => loadCohortOptions(anchorParams);
      const [cohortId, cohortVersion] = ui.cohort.value.split("|");
      if (!cohortId) return;
      const params = new win.URLSearchParams(Object.assign({
        cohort_selector_id: cohortId, cohort_version: cohortVersion,
      }, scopeParams(), anchorParams || {}));
      const payload = await runStage("measurement, anchor and policy selectors",
        () => call("GET", `/api/v1/longitudinal/selectors?${params}`));
      if (!payload) return;
      const previousMeasurement = ui.measurement.value;
      catalog = payload;
      fill(ui.measurement, payload.measurement_options.map((item, index) => option(String(index),
        `${words(item.measurement.quantity_id)} (${item.measurement.unit}); ${words(item.projection_family)} policy version ${item.projection_policy_version}; standalone values ${item.standalone_values}${item.missing_prerequisite ? `, prerequisite ${PREREQUISITES[item.missing_prerequisite]} missing` : ""}`)), "Choose a measurement");
      if (previousMeasurement) ui.measurement.value = previousMeasurement;
      const previousPolicy = ui.anchorPolicy.value;
      fill(ui.anchorPolicy, payload.anchor_policies.map((item) => option(
        `${item.anchor_policy_selector_id}|${item.anchor_policy_version}`,
        `${item.anchor_policy_selector_id.slice(0, 26)} approval ${item.anchor_policy_version} (${words(item.authority_state)}; ${text(item.eligible_count)} eligible)`)), "Choose an anchor policy");
      if (previousPolicy) ui.anchorPolicy.value = previousPolicy;
      fill(ui.d09, payload.d09_policies.map((item) => option(
        `${item.d09_policy_selector_id}|${item.d09_policy_version}`,
        `${item.d09_policy_selector_id.slice(0, 22)} version ${item.d09_policy_version} (${words(item.authority_state)})`)), "Choose a D09 policy");
      const candidates = payload.anchor_candidates ? payload.anchor_candidates.candidates : [];
      // No implicit first/latest anchor: the operator must choose one.
      fill(ui.anchor, candidates.map((item) => {
        const node = option(item.anchor_selector_id,
          `${item.alias}: timepoint ${item.biological_timepoint_ordinal}, offset ${item.time_offset_seconds} s, method ${item.method_version}, ${words(item.eligibility_state)}`);
        node.disabled = item.eligibility_state !== "eligible";
        return node;
      }), "Choose an approved anchor");
      setState("empty");
      ui.status.textContent = "Selections loaded; show the version diff before results.";
      refreshResultsButton();
    };

    const showDiff = async () => {
      retryStep = showDiff;
      const request = buildRequest();
      if (!request) {
        ui.status.textContent = "Choose every selector first; there is no implicit anchor.";
        return;
      }
      const params = new win.URLSearchParams({
        cohort_selector_id: request.cohort_selector_id,
        cohort_version: String(request.cohort_version),
        family: request.measurement.family,
        quantity_id: request.measurement.quantity_id,
        unit: request.measurement.unit,
      });
      const payload = await runStage("version diff", () => call("GET", `/api/v1/longitudinal/diff?${params}`));
      if (!payload) return;
      ui.diff.hidden = false;
      ui.diffBody.replaceChildren();
      renderVersionDiff(doc, ui.diffBody, payload.diff);
      diffShownFor = requestKey(request);
      setState("empty");
      ui.status.textContent = "Version diff shown. Confirm the exact version, then show results.";
      refreshResultsButton();
    };

    const openDrawer = async (row, opener) => {
      drawerOpener = opener;
      const mode = drawerMode(win.innerWidth || 1280);
      ui.results.dataset.drawer = mode;
      ui.drawer.hidden = false;
      ui.drawer.setAttribute("aria-modal", mode === "sheet" ? "true" : "false");
      renderDrawer(doc, ui, row, null);
      if (mode === "overlay" && opener && opener.scrollIntoView) opener.scrollIntoView({ block: "nearest" });
      ui.drawerClose.focus();
      if (!currentRequest) return;
      const result = await call("POST", "/api/v1/longitudinal/source", { request: currentRequest, row_ordinal: row.row_ordinal });
      if (result.status === 200 && result.payload) renderDrawer(doc, ui, result.payload.row, result.payload);
      else if (result.status === 403) { setState("permission-denied"); showProblem("permission_denied"); }
    };
    const closeDrawer = () => {
      ui.drawer.hidden = true;
      delete ui.results.dataset.drawer;
      if (drawerOpener && drawerOpener.focus) drawerOpener.focus();
      drawerOpener = null;
    };

    const presentWorkspace = (response, authorityLabel, forcedState) => {
      const workspace = response.workspace || response;
      ui.results.hidden = false;
      renderWorkspace(doc, ui, workspace, { authorityLabel }, openDrawer);
      const state = forcedState || classifyWorkspace(workspace);
      setState(state);
      ui.status.textContent = state === "empty"
        ? `Empty: ${emptyReason(workspace)} Denominator counts are retained. Next safe action: adjust filters or choose another anchor.`
        : `${words(state)}: ${workspace.rows.length} of ${workspace.total_row_count} source rows shown.`;
      const save = response.save ? response.save.state : "registry_absent";
      ui.save.disabled = save !== "available" || Boolean(forcedState);
      ui.saveState.textContent = save === "available"
        ? "Save publishes an immutable comparison; the receipt appears only after the final authority fence."
        : `Save is disabled: ${REMEDIATIONS[save] || words(save)}`;
    };

    const showResults = async () => {
      retryStep = showResults;
      const request = buildRequest();
      if (!request || requestKey(request) !== diffShownFor) return;
      const payload = await runStage("workspace build", () => call("POST", "/api/v1/longitudinal/workspace", { request }));
      if (!payload) return;
      currentRequest = request;
      lastView = { kind: "workspace" };
      ui.receipt.replaceChildren();
      presentWorkspace(payload, "current: built from live registries at one composite snapshot");
    };

    const saveComparison = async () => {
      retryStep = saveComparison;
      if (!currentRequest) return;
      ui.save.disabled = true;
      const payload = await runStage("save comparison", () => call("POST", "/api/v1/longitudinal/save", { request: currentRequest }));
      if (!payload) return;
      setState("success");
      ui.status.textContent = "Saved after transactional publication and the final authority fence.";
      ui.receipt.replaceChildren(el(doc, "p",
        `Saved ${payload.saved_selector_id} version ${payload.comparison_version}; object ${payload.object_sha256}; ${payload.applied ? "published" : "already saved (exact retry)"}. Saving does not authorize export.`));
      loadSaved();
    };

    const loadSaved = async () => {
      let result;
      try {
        result = await call("GET", "/api/v1/longitudinal/saved");
      } catch (_) {
        return;
      }
      ui.savedList.replaceChildren();
      if (result.status !== 200 || !result.payload) return;
      result.payload.records.forEach((record) => {
        const item = el(doc, "li", `${record.saved_selector_id} version ${record.comparison_version} (${words(record.authority_state)}) `);
        const button = el(doc, "button", `Reopen version ${record.comparison_version}`, { type: "button" });
        button.addEventListener("click", () => reopen(record));
        item.append(button);
        ui.savedList.append(item);
      });
      if (!result.payload.records.length) ui.savedList.append(el(doc, "li", `No saved comparisons (${words(result.payload.save.state)}).`));
    };

    // Reopen: the diff is fetched and shown first; results only after confirmation.
    const reopen = async (record) => {
      retryStep = () => reopen(record);
      hideResults();
      const body = { saved_selector_id: record.saved_selector_id, comparison_version: record.comparison_version, stage: "diff" };
      const payload = await runStage("reopen diff", () => call("POST", "/api/v1/longitudinal/reopen", body));
      if (!payload) return;
      ui.diff.hidden = false;
      renderReopenDiff(doc, ui.diffBody, payload.diff);
      const confirm = el(doc, "button", "Show reopened results", { type: "button" });
      confirm.addEventListener("click", async () => {
        const results = await runStage("reopen results", () => call("POST", "/api/v1/longitudinal/reopen", Object.assign({}, body, { stage: "results" })));
        if (!results) return;
        lastView = { kind: "reopen", record };
        renderReopenDiff(doc, ui.diffBody, results.diff);
        if (results.current_workspace) {
          currentRequest = null;
          presentWorkspace({ workspace: results.current_workspace, save: { state: "registry_absent" } }, "current: saved comparison replays exactly");
          return;
        }
        ui.results.hidden = false;
        ui.rows.replaceChildren();
        renderHistorical(doc, ui.covariates, results.historical_commitments);
        ui.chart.replaceChildren(el(doc, "p", "Stale: no current segments are drawn for a stale saved comparison.", { class: "help" }));
        setState("stale");
        ui.status.textContent = "Stale: authority differs from the saved comparison. Comparisons and segments are suppressed. Required action: start a new comparison at current authority.";
        ui.save.disabled = true;
      });
      ui.diffBody.append(confirm);
      setState("stale");
      ui.status.textContent = payload.diff.comparison_state === "current"
        ? "Diff shown: the saved comparison replays at current authority. Confirm to show results."
        : "Diff shown: the saved comparison is stale. Confirm to inspect it without current comparisons.";
      if (payload.diff.comparison_state === "current") setState("success");
    };

    ui.cohort.addEventListener("change", () => { diffShownFor = null; loadCohortOptions(); });
    ui.scope.addEventListener("change", () => {
      diffShownFor = null;
      catalog = null;
      ui.measurement.value = "";
      loadCohortOptions();
    });
    ui.measurement.addEventListener("change", () => { diffShownFor = null; refreshResultsButton(); });
    ui.anchorPolicy.addEventListener("change", () => {
      diffShownFor = null;
      const [id, version] = ui.anchorPolicy.value.split("|");
      if (id) loadCohortOptions({ anchor_policy_selector_id: id, anchor_policy_version: version });
    });
    ui.anchor.addEventListener("change", refreshResultsButton);
    ui.d09.addEventListener("change", refreshResultsButton);
    ui.journey.addEventListener("change", refreshResultsButton);
    ui.showDiff.addEventListener("click", showDiff);
    ui.journey.addEventListener("submit", (event) => { event.preventDefault(); showResults(); });
    ui.save.addEventListener("click", saveComparison);
    ui.retry.addEventListener("click", () => { if (retryStep) retryStep(); });
    ui.drawerClose.addEventListener("click", closeDrawer);
    ui.drawer.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { closeDrawer(); return; }
      if (event.key !== "Tab" || ui.drawer.getAttribute("aria-modal") !== "true") return;
      const target = containFocus(ui.drawer, doc.activeElement, event.shiftKey);
      if (target) { event.preventDefault(); target.focus(); }
    });
    // Navigating away aborts in-flight work (no duplicate builds); returning
    // restores only after fetching a fresh revision.
    doc.addEventListener("visibilitychange", () => {
      if (doc.visibilityState === "hidden") {
        if (inflight) inflight.abort();
        stopTicker();
      } else if (lastView && lastView.kind === "workspace" && currentRequest) {
        showResults();
      }
    });
    win.addEventListener("traceback:longitudinal", (event) => {
      csrf = event.detail && event.detail.csrfToken;
      section.hidden = false;
      loadCohorts();
    });
  };

  return {
    STATES,
    SLOW_STAGE_MS,
    ACTIONS,
    OPERATOR_ENTERED,
    classifyWorkspace,
    chartModel,
    comparisonText,
    containFocus,
    drawerMode,
    emptyReason,
    focusables,
    problemFor,
    renderReopenDiff,
    renderHistorical,
    renderVersionDiff,
    renderWorkspace,
    renderDrawer,
    valueText,
    install,
  };
});
