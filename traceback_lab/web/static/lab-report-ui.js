// Explicit, fresh report capture for local ResearchReportV2 export and print.
"use strict";

(() => {
  const core = globalThis.TracebackLabReport;
  const lab = globalThis.TracebackLab;
  if (!core || !lab) return;
  const { h, state } = lab;
  let captured = null;
  let pending = null;
  let panel = null;
  let objectUrl = null;
  let releaseTimer = null;
  let requestToken = 0;

  function releaseUrl() {
    if (releaseTimer) clearTimeout(releaseTimer);
    releaseTimer = null;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }

  function invalidate() {
    requestToken += 1;
    if (pending) pending.abort();
    pending = null;
    captured = null;
    releaseUrl();
    if (panel?.isConnected) renderPanel();
  }

  function selected(id12, revision) {
    return state.sample?.found === true && state.sample.id12 === id12 &&
      state.navigationRevision === revision &&
      !state.sample.state &&
      document.body.dataset.disconnected !== "1" && state.sessionExpired !== true;
  }

  function safeMetadata(value) {
    if (typeof value !== "string" || value.length > 1200 ||
        /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\\]/.test(value) ||
        /(?:password|secret|token|api[_ -]?key|bearer|AKIA|sk-[A-Za-z0-9]{8}|[ACGTN]{30})/i.test(value) ||
        /(?:^|[\s(:])\/[A-Za-z][\w.-]*(?:\/[\w.-]+)+/.test(value) ||
        /\b[0-9a-f]{32,}\b/i.test(value)) return null;
    return value;
  }

  function metadataList(value) {
    if (typeof value === "string") return safeMetadata(value) || null;
    if (!Array.isArray(value)) return null;
    return value.slice(0, 12).map(safeMetadata).filter(Boolean).join("; ") || null;
  }

  function printContext(sample, report) {
    const catalog = new Map((sample.catalog || []).filter((row) => row && typeof row.algorithm_id === "string")
      .map((row) => [row.algorithm_id, row]));
    const algorithms = new Map((sample.algorithms || []).filter((row) => row && typeof row.algorithm_id === "string")
      .map((row) => [row.algorithm_id, row]));
    const method = {};
    const metric = {};
    for (const row of report.metrics) {
      if (!method[row.algorithm_id]) {
        const source = catalog.get(row.algorithm_id) || {};
        method[row.algorithm_id] = {
          question: safeMetadata(source.question), description: safeMetadata(source.description),
          limitations: metadataList(source.limitations), requirements: metadataList(source.requirements),
        };
      }
      const source = algorithms.get(row.algorithm_id)?.metrics?.find((item) => item.metric_id === row.metric_id) || {};
      const entry = source.entry || {};
      const reference = entry.reference;
      let referenceText = null;
      if (reference && typeof reference === "object") {
        const kind = safeMetadata(reference.kind);
        const sourceName = safeMetadata(reference.source);
        referenceText = [kind, sourceName].filter(Boolean).join(": ") || null;
      }
      const guide = globalThis.TracebackLabViews?.signalDefinition?.(row.algorithm_id, row.metric_id) || null;
      const nextSteps = entry.next_steps && typeof entry.next_steps === "object"
        ? [metadataList(entry.next_steps.analytical), metadataList(entry.next_steps.clinical_setting)]
          .filter(Boolean).join("; ") || null : null;
      metric[`${row.algorithm_id}/${row.metric_id}`] = {
        support: row.recorded_support_n,
        reference: referenceText,
        confounders: metadataList(entry.confounders), nextSteps,
        guide: guide ? {
          definition: safeMetadata(guide.definition), denominator: safeMetadata(guide.denominator),
          limitation: safeMetadata(guide.limitation),
        } : null,
      };
    }
    return { method, metric };
  }

  function currentCapture() {
    if (state.sessionExpired === true) {
      const hadCapture = !!captured;
      captured = null;
      releaseUrl();
      if (hadCapture && panel?.isConnected) queueMicrotask(renderPanel);
      return null;
    }
    return captured && state.sample?.id12 === captured.report.sample_id ? captured : null;
  }

  function message(text, problem = false) {
    if (!panel?.isConnected) return;
    const status = panel.querySelector("[data-report-status]");
    if (status) { status.textContent = text; status.classList.toggle("problem", problem); }
  }

  async function prepare() {
    if (pending) return;
    const id12 = state.sample?.id12;
    const revision = state.navigationRevision;
    if (!id12 || !selected(id12, revision)) {
      message("Select an available sample and reconnect before preparing a report.", true);
      return;
    }
    invalidate();
    const token = ++requestToken;
    const controller = new AbortController();
    pending = controller;
    renderPanel();
    try {
      // This is the only sample request in the prepare action. No page cache,
      // artifact or status response can authorize an export.
      const sample = await lab.requestJson(`/api/samples/${id12}`, {
        signal: controller.signal, timeoutMs: 10000,
        isCurrent: () => token === requestToken && selected(id12, revision),
      });
      if (token !== requestToken || !selected(id12, revision)) return;
      if (!sample || sample.found !== true || sample.id12 !== id12) {
        throw Object.assign(new Error("The selected sample is unavailable for reporting."), { code: "sample_unavailable" });
      }
      const report = core.project(sample, new Date().toISOString(), lab.formatResultMetric);
      const context = printContext(sample, report);
      if (token !== requestToken || !selected(id12, revision)) return;
      captured = { report, context };
      renderPanel();
      message("Fresh report captured. Review its time and input before downloading or printing.");
    } catch (error) {
      if (token !== requestToken) return;
      const known = new Set(["invalid_report_identity", "no_report_rows", "report_too_many_rows", "report_too_large"]);
      const copy = error?.kind === "timeout" ? "Report preparation timed out after 10 seconds. Try again."
        : error?.kind === "session" ? "Session expired. Open a fresh Lab link from the serving terminal."
          : known.has(error?.code) ? error.message
            : error?.kind === "invalid" ? "The fresh sample response was invalid. Try again."
              : "The selected sample could not be refreshed. Try again.";
      message(copy, true);
    } finally {
      if (pending === controller) pending = null;
      if (token === requestToken) renderPanel();
    }
  }

  function download(extension) {
    const capture = currentCapture();
    if (!capture) { message("Prepare a fresh report first.", true); return; }
    let link;
    try {
      const contents = extension === "json" ? core.json(capture.report) : core.csv(capture.report);
      const mime = extension === "json" ? "application/json;charset=utf-8" : "text/csv;charset=utf-8";
      releaseUrl();
      objectUrl = URL.createObjectURL(new Blob([contents], { type: mime }));
      link = h("a", { href: objectUrl, download: core.filename(capture.report, extension) });
      document.body.append(link);
      link.click();
      message("Download started. Check your browser for the saved file.");
      releaseTimer = setTimeout(releaseUrl, 60000);
    } catch (error) {
      releaseUrl();
      message(error?.code === "report_too_large"
        ? `The ${extension.toUpperCase()} report exceeds the 5 MiB download limit. Select a smaller sample and prepare a new report. No file was saved.`
        : "The download could not be started. Try again.", true);
    } finally {
      link?.remove();
    }
  }

  function field(label, value) {
    return h("p", {}, h("strong", {}, `${label}: `), value == null || value === "" ? "unavailable" : String(value));
  }

  function provenanceText(value) {
    return value == null || value === "" ? "unavailable" : String(value);
  }

  function printTree(capture) {
    const { report, context } = capture;
    const root = h("section", { id: "lab-report-print", "aria-label": "Captured research report",
      "data-example": String(report.example) },
      h("div", { class: "report-print-marks" }, "RESEARCH USE ONLY · NOT CLIA VALIDATED · NOT A DIAGNOSIS",
        report.example ? " · SYNTHETIC EXAMPLE" : ""),
      h("div", { class: "report-print-limits" },
        "Visible registered scalar metrics only; hidden steps, arrays, artifacts and taxon detail omitted. Values may differ from the earlier page."),
      h("h1", {}, "Traceback Lab research report"),
      field("Schema version", report.schema_version),
      field("Captured UTC", report.generated_at), field("Sample", report.sample_id),
      field("Input", report.full_input_id), field("Source", report.source_id),
      field("Recorded date (UTC)", report.recorded_utc), field("Recorded date kind", report.recorded_utc_kind),
      field("Interpretation version", report.interpretation_version),
      field("Cohort version", report.cohort_version),
      h("p", {}, "Freshly captured report; values may differ from the earlier page."),
      h("h2", {}, "Limitations"), h("ul", {}, ...report.limitations.map((item) => h("li", {}, item))));
    const groups = new Map();
    for (const row of report.metrics) {
      if (!groups.has(row.algorithm_id)) groups.set(row.algorithm_id, []);
      groups.get(row.algorithm_id).push(row);
    }
    for (const [algorithmId, rows] of groups) {
      const meta = context.method[algorithmId] || {};
      const section = h("section", { class: "report-print-method" }, h("h2", {}, algorithmId),
        field("Question", meta.question), field("Method", meta.description),
        field("Requirements", meta.requirements), field("Limitations", meta.limitations));
      for (const row of rows) {
        const detail = context.metric[`${algorithmId}/${row.metric_id}`] || {};
        section.append(h("article", { class: "report-print-metric" }, h("h3", {}, row.label),
          field("Metric ID", row.metric_id),
          field("Displayed value", `${row.display_value}${row.display_unit === "%" ? "%" : row.display_unit ? ` ${row.display_unit}` : ""}`),
          field("Exact API value", row.raw_value == null ? "unavailable" : `${row.raw_value}${row.raw_unit ? ` ${row.raw_unit}` : " (unit not declared)"}`),
          field("Interpretation", row.interpretation_state),
          field("Interpretation reason", row.interpretation_reason),
          field("Recorded support count", detail.support), field("Reference", detail.reference),
          field("Confounders", detail.confounders), field("Next steps", detail.nextSteps),
          field("Measurement", detail.guide?.definition),
          field("Denominator / filter", detail.guide?.denominator),
          field("Measurement limitation", detail.guide?.limitation),
          h("div", { class: "report-print-provenance", "aria-label": "Validated result provenance" },
            h("p", {}, h("strong", {}, "Result provenance — "),
              `Tool: ${provenanceText(row.tool)}; Tool version: ${provenanceText(row.tool_version)}; `,
              `Computed UTC: ${provenanceText(row.computed_at)}.`),
            h("p", {},
              `Parameters SHA-256: ${provenanceText(row.parameters_sha256)}; `,
              `Attempt ID: ${provenanceText(row.attempt_id)}; `,
              `Reference asset ID: ${provenanceText(row.reference_id)}.`))));
      }
      root.append(section);
    }
    return root;
  }

  function printReport() {
    const capture = currentCapture();
    if (!capture) { message("Prepare a fresh report first.", true); return; }
    let tree;
    let cleanup;
    let cleanupTimer;
    try {
      tree = printTree(capture);
      document.body.append(tree);
      cleanup = () => {
        tree?.remove();
        window.removeEventListener("afterprint", cleanup);
        if (cleanupTimer) clearTimeout(cleanupTimer);
      };
      window.addEventListener("afterprint", cleanup, { once: true });
      cleanupTimer = setTimeout(cleanup, 120000);
      window.print();
    } catch (_) {
      if (cleanup) cleanup(); else tree?.remove();
      message("The print view could not be opened. Try again.", true);
    }
  }

  function renderPanel() {
    if (!panel?.isConnected) return;
    const capture = currentCapture();
    // Retain the live region so its error style and announcement survive a rerender.
    const status = panel.querySelector("[data-report-status]") ||
      h("p", { "data-report-status": "", role: "status", "aria-live": "polite" });
    panel.replaceChildren(h("h2", {}, "Export / print"),
      h("p", {}, "Capture a fresh selected result for a research report. This request may show values different from the page above."),
      h("button", { type: "button", class: "control", "data-requires-server": "", disabled: !!pending,
        onclick: prepare }, pending ? "Preparing report…" : "Prepare Export / Print"),
      status,
      capture ? h("div", { class: "report-preview" },
        h("h3", {}, "Captured report preview"),
        field("Captured UTC", capture.report.generated_at),
        field("Selected sample", capture.report.sample_id),
        field("Input", capture.report.full_input_id),
        field("Included scalar rows", capture.report.metrics.length),
        h("p", {}, "Freshly captured report; values may differ from the earlier page."),
        h("p", {}, "Research use only. Not CLIA-validated. Not a diagnosis.",
          capture.report.example ? " SYNTHETIC EXAMPLE." : ""),
        h("ul", {}, ...capture.report.limitations.map((item) => h("li", {}, item))),
        h("div", { class: "report-actions" },
          h("button", { type: "button", class: "control", onclick: () => download("json") }, "Download JSON"),
          h("button", { type: "button", class: "control", onclick: () => download("csv") }, "Download CSV"),
          h("button", { type: "button", class: "control", onclick: printReport }, "Print captured report"))) : "");
  }

  function mount(main, sample) {
    if (captured && captured.report.sample_id !== sample.id12) invalidate();
    panel = h("section", { class: "panel report-export-panel", "aria-label": "Export or print a research report" });
    main.append(panel);
    renderPanel();
  }

  globalThis.TracebackLabReportUI = {
    mount,
    selectionChanged: (id12) => {
      if (captured && captured.report.sample_id !== id12 && captured.report.source_id !== id12) invalidate();
      if (pending) invalidate();
    },
    invalidate,
  };
})();
