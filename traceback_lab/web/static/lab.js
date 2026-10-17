// Traceback Lab page.  Same-origin script only (CSP: script-src 'self', no
// inline script or style).  Text is set with textContent, never innerHTML.
// Research use only. Not CLIA-validated. Not a diagnosis.
"use strict";

(function () {
  // Record CSP violations first, so the headless CSP test can read them.
  const violations = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    violations.push(`${event.violatedDirective} ${event.blockedURI || ""}`.trim());
    document.body.dataset.cspViolations = String(violations.length);
    document.body.dataset.cspLast = violations[violations.length - 1];
  });

  const POLL_MS = 15000;
  const MISSED_LIMIT = 3;
  const LENSES = [
    // Fixed order; a lens that is not built yet is absent (S6c adds the rest).
    { id: "trust-qc", label: "Trust/QC" },
  ];
  const state = {
    samples: [],
    sample: null,
    generationAtLoad: null,
    lastStatus: null,
    missed: 0,
    lastOk: null,
    route: { name: "home" },
    loading: true,
  };
  window.TracebackLab = { state, LENSES, h, render, chart, dataTable, fmt };

  // -- tiny DOM helper -----------------------------------------------------------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function fmt(value, unit) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "number") {
      const text = Number.isInteger(value) ? value.toLocaleString("en-US") : (Math.abs(value) < 0.01 && value !== 0 ? value.toExponential(2) : value.toFixed(Math.abs(value) < 1 ? 3 : 1));
      return unit && unit !== "fraction" ? `${text} ${unit}` : text;
    }
    if (typeof value === "boolean") return value ? "yes" : "no";
    return String(value);
  }

  // -- session ------------------------------------------------------------------------
  async function exchangeBootstrap() {
    const params = new URLSearchParams(location.hash.slice(1));
    const code = params.get("bootstrap");
    if (!code) return;
    // An optional start route travels with the one-use link (e.g. for a deep link).
    const route = params.get("route");
    const safe = route && /^\/[A-Za-z0-9/?=._-]{0,120}$/.test(route) ? route : "/";
    history.replaceState(null, "", location.pathname + "#" + safe);
    const response = await fetch("/api/v1/session/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bootstrap: code }),
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("bootstrap");
  }

  async function api(path) {
    const response = await fetch(path, { credentials: "same-origin" });
    if (response.status === 401) throw Object.assign(new Error("session"), { session: true });
    const payload = await response.json();
    if (!response.ok && !(path.startsWith("/api/samples/") && response.status === 404)) throw new Error(`http ${response.status}`);
    return payload;
  }

  // -- routes -------------------------------------------------------------------------
  function parseRoute() {
    const hash = location.hash.replace(/^#/, "") || "/";
    const [pathPart, query = ""] = hash.split("?");
    const params = new URLSearchParams(query);
    let match = pathPart.match(/^\/sample\/([0-9a-f]{12,64})\/([a-z-]+)$/);
    if (match) return { name: "lens", id12: match[1], lens: match[2] };
    if (pathPart === "/" || pathPart === "") {
      const sample = params.get("sample");
      if (sample && !/^[0-9a-f]{12,64}$/.test(sample)) return { name: "unknown", id12: sample };
      return { name: "home", id12: sample };
    }
    if (pathPart === "/about/clia") return { name: "clia" };
    return { name: "unknown", id12: null };
  }

  async function loadSample(id12) {
    if (!id12) { state.sample = null; return; }
    if (state.sample && state.sample.id12 === id12) return;
    const payload = await api(`/api/samples/${id12}`);
    state.sample = payload.found ? payload : { found: false, id12, candidates: payload.candidates || [] };
  }

  async function navigate() {
    state.route = parseRoute();
    const route = state.route;
    const target = route.id12 || (state.samples[0] && state.samples[0].id12) || null;
    if (route.name === "home" || route.name === "lens") await loadSample(target);
    render();
    const title = document.querySelector("main h1");
    if (title) { title.setAttribute("tabindex", "-1"); title.focus({ preventScroll: false }); }
  }

  // -- render -------------------------------------------------------------------------
  function setStatus(text, action) {
    const status = document.getElementById("status");
    status.replaceChildren(text ? text : "");
    if (action) status.append(" ", h("button", { type: "button", class: "control reload", onclick: () => location.reload() }, action));
  }

  function renderSwitcher() {
    const select = document.getElementById("sample-switcher");
    select.replaceChildren(
      ...state.samples.map((row) =>
        h("option", { value: row.id12, selected: state.sample && state.sample.id12 === row.id12 },
          `${row.id12} · ${row.run_utc ? row.run_utc.slice(0, 10) : "—"} · ${row.depth_x !== null ? row.depth_x.toFixed(2) + "x" : "—"}${row.example ? " · EXAMPLE (synthetic)" : ""}`)
      )
    );
  }

  function renderNav() {
    const nav = document.getElementById("lenses");
    const sample = state.sample && state.sample.found ? state.sample : null;
    nav.replaceChildren();
    // Lens tabs belong to a sample's lens routes; the landing page has none (spec layout).
    nav.hidden = !sample || state.route.name !== "lens";
    if (!sample || nav.hidden) return;
    const current = state.route.name === "lens" ? state.route.lens : LENSES[0].id;
    for (const lens of LENSES) {
      let label = lens.label;
      if (lens.id === "trust-qc") {
        const failed = sample.algorithms.filter((a) => a.status === "failed").length;
        if (failed) label += ` (${failed} failed)`;
      }
      if (lens.count) label += lens.count(sample);
      nav.append(h("a", { href: `#/sample/${sample.id12}/${lens.id}`, "aria-current": lens.id === current ? "page" : null }, label));
    }
  }

  function render() {
    renderSwitcher();
    renderNav();
    const main = document.getElementById("main");
    main.replaceChildren();
    const route = state.route;
    if (state.loading) { main.append(h("h1", { class: "view-title" }, "Loading…"), h("p", {}, "Loading Lab results…")); return; }
    if (!state.samples.length && route.name !== "clia") {
      main.append(...emptyState());
      return;
    }
    if (route.name === "clia" && window.TracebackLabViews && window.TracebackLabViews.clia) { main.append(...window.TracebackLabViews.clia()); return; }
    const sample = state.sample;
    if (route.name === "unknown" || !sample || !sample.found) {
      const shown = route.id12 || (sample && sample.id12) || "";
      main.append(h("h1", { class: "view-title" }, "Unknown sample"),
        h("p", {}, `No Lab sample matches ${shown}. `, h("a", { href: "#/" }, "Back to Lab home")));
      if (sample && sample.candidates && sample.candidates.length) main.append(h("p", {}, `Candidates: ${sample.candidates.join(", ")}`));
      return;
    }
    const lens = route.name === "lens" ? route.lens : null;
    const views = window.TracebackLabViews || {};
    const appendSample = (nodes) => {
      main.append(...nodes);
      if (sample.excluded_chunks && sample.excluded_chunks.length) {
        const notice = h("div", { class: "status-line" },
          h("p", {}, `Provisional: this input excludes ${sample.excluded_chunks.length} truncated chunks; depth reflects the reads present.`),
          h("details", {}, h("summary", {}, `Excluded chunk indexes (${sample.excluded_chunks.length})`),
            h("p", {}, `Excluded chunk indexes: ${sample.excluded_chunks.join(", ")}.`)));
        const heading = main.querySelector("h1");
        if (heading) heading.after(notice); else main.prepend(notice);
      }
    };
    if (route.name === "home" && views.landing) { appendSample(views.landing(sample)); return; }
    const chosen = lens || LENSES[0].id;
    if (chosen === "trust-qc") { appendSample(trustQc(sample)); return; }
    if (views[chosen]) { appendSample(views[chosen](sample)); return; }
    main.append(h("h1", { class: "view-title" }, "Unknown sample"), h("p", {}, "No such lens. ", h("a", { href: "#/" }, "Back to Lab home")));
  }

  function emptyState() {
    const copy = (text) => h("p", {}, h("code", {}, text), " ", h("button", { type: "button", class: "control", onclick: () => navigator.clipboard && navigator.clipboard.writeText(text) }, "Copy"));
    return [
      h("h1", { class: "view-title" }, "Traceback Lab"),
      h("p", { class: "empty" }, "No Lab runs under this server's ROOT (printed in the terminal where `lab serve` started). Check setup, try the synthetic example, or run your own chunk directory with the same ROOT, then reload."),
      copy("traceback lab doctor"),
      copy("traceback lab run --example"),
      h("p", {}, h("code", {}, "traceback lab run <chunk dir>")),
    ];
  }

  // -- charts ---------------------------------------------------------------------------
  const chartCell = (value) => typeof value === "number" && Number.isFinite(value) ? String(value) : fmt(value);
  function dataTable(caption, headers, rows) {
    const table = h("table", { class: "data-table" },
      h("caption", {}, caption),
      h("thead", {}, h("tr", {}, ...headers.map((name) => h("th", { scope: "col" }, name)))),
      h("tbody", {}, ...rows.map((row) => h("tr", {}, ...row.map((cell) => h("td", {}, chartCell(cell)))))));
    return h("details", { class: "chart-table" }, h("summary", {}, "Data table"), h("div", { class: "table-wrap" }, table));
  }

  // ECharts with animation off, a text aria-label and an adjacent data table.
  function chart(label, option, table) {
    const box = h("div", { class: "echart", role: "img", "aria-label": label });
    const wrap = h("div", { class: "chart-wrap" }, box, table);
    requestAnimationFrame(() => {
      try {
        if (!window.echarts) throw new Error("chart library unavailable");
        const instance = window.echarts.init(box, null, { renderer: "canvas" });
        instance.setOption(Object.assign({ animation: false }, option));
        box.dataset.rendered = "1";
      } catch (error) {
        box.replaceWith(h("div", { class: "problem" }, `Chart could not be drawn (${error.message}). The data table below has the same numbers.`));
        const details = wrap.querySelector("details");
        if (details) details.open = true;
      }
    });
    return wrap;
  }

  async function fetchArtifact(sample, label, algorithmId, panelId) {
    const response = await fetch(`/api/samples/${sample.id12}/artifact/${label}/${algorithmId}/${panelId}`, { credentials: "same-origin" });
    if (!response.ok) throw new Error("data reference invalid");
    return response.json();
  }

  function sizeDensity(sample) {
    const section = h("section", { class: "panel", "aria-labelledby": "size-density-title" }, h("h2", { id: "size-density-title" }, "Fragment-size density"));
    const algorithm = sample.algorithms.find((a) => a.algorithm_id === "fragment-size");
    if (!algorithm || algorithm.status !== "ok") { section.append(h("p", { class: "empty" }, "fragment-size has no result for this sample.")); return section; }
    const slot = h("div", {}, h("p", {}, "Loading…"));
    section.append(slot);
    fetchArtifact(sample, "full", "fragment-size", "size-density").then((data) => {
      const points = data.span_bp.map((x, i) => [x, data.count[i]]).filter((p) => p[0] >= 50 && p[0] <= 700);
      const mode = (algorithm.metrics.find((m) => m.metric_id === "mode_bp") || {}).value;
      const table = dataTable("Molecules by aligned span (bp), 50-700 bp", ["Span (bp)", "Molecules"], points);
      slot.replaceWith(chart(
        `Fragment-size density, 50 to 700 bp; mode ${mode} bp; 167 bp marked.`,
        {
          grid: { left: 60, right: 20, top: 20, bottom: 40 },
          xAxis: { type: "value", name: "bp", min: 50, max: 700 },
          yAxis: { type: "value", name: "molecules" },
          series: [{ type: "line", showSymbol: false, data: points, lineStyle: { color: "#1f5f8b" },
            markLine: { symbol: "none", data: [{ xAxis: 167, label: { formatter: "167 bp" } }] } }],
        },
        table,
      ));
    }).catch((error) => {
      // No panel data reached the page, so there is no data table to open;
      // the summary metrics stay in the table below.
      const rows = algorithm.metrics.map((m) => [m.label, m.value]);
      slot.replaceWith(h("div", {},
        h("div", { class: "problem" }, `Chart could not be drawn (${error.message}). The summary numbers are in the table below.`),
        h("div", { class: "table-wrap" }, h("table", { class: "data-table" }, h("caption", {}, "fragment-size summary"),
          h("tbody", {}, ...rows.map(([label, value]) => h("tr", {}, h("th", { scope: "row" }, label), h("td", {}, fmt(value)))))))));
    });
    return section;
  }

  // -- Trust / QC ---------------------------------------------------------------------
  function trustQc(sample) {
    const depth = sample.depth ? sample.depth.approx_depth_x : null;
    const out = [
      h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Trust/QC`),
      h("h1", { class: "view-title" }, "Trust / QC"),
    ];
    if (sample.excluded_chunks && sample.excluded_chunks.length) {
      out.push(h("p", { class: "help" }, "See the excluded chunk indexes in the provisional input notice above."));
    }
    // Depth vs minimum-depth bars.
    const visible = sample.algorithms.filter((a) => !a.hidden);
    const maxDepth = Math.max(depth || 0, ...visible.map((a) => a.min_depth_x || 0)) || 1;
    const rows = visible.map((a) => {
      const bar = h("div", { class: "depth-bar", "aria-hidden": "true" });
      const fill = h("div", { class: "fill" });
      fill.style.width = `${Math.min(100, ((depth || 0) / maxDepth) * 100)}%`;
      bar.append(fill);
      if (a.min_depth_x) { const tick = h("div", { class: "tick" }); tick.style.left = `${(a.min_depth_x / maxDepth) * 100}%`; bar.append(tick); }
      const text = a.min_depth_x
        ? `needs ${a.min_depth_x}x; this run ${depth !== null ? depth.toFixed(3) : "—"}x → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status === "failed" ? "failed" : "ran"}`
        : `any depth → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status}`;
      return h("tr", {}, h("th", { scope: "row" }, a.algorithm_id), h("td", {}, bar), h("td", {}, text));
    });
    out.push(h("section", { class: "panel" }, h("h2", {}, "Depth against each algorithm's minimum"),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Algorithm"), h("th", {}, "Depth"), h("th", {}, "Verdict"))), h("tbody", {}, ...rows)))));
    // Run table.
    const active = state.lastStatus && state.lastStatus.active;
    const interrupted = state.lastStatus && state.lastStatus.interrupted;
    const pending = [active, interrupted].find((run) => run && run.step && run.source_id === sample.source_id &&
      run.input_id === sample.full_input_id && !sample.algorithms.some((a) => a.algorithm_id === run.step));
    const runRows = sample.algorithms.map((a) => {
      const status = a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status;
      const matches = (run) => run && run.step === a.algorithm_id && run.source_id === sample.source_id && run.input_id === a.provenance.input_id;
      const runNote = matches(active) ? "latest run in progress" : matches(interrupted) ? "latest run interrupted" : null;
      const cells = [h("th", { scope: "row" }, a.algorithm_id + (a.hidden ? " (intermediate)" : "")),
        h("td", { class: a.status === "failed" ? "problem" : null }, status,
          runNote ? h("span", { class: "state-chip run-note" }, runNote) : ""),
        h("td", { class: "num" }, a.duration_s !== null && a.duration_s !== undefined ? `${a.duration_s.toFixed(1)} s` : "—")];
      const logLine = h("span", { class: "help", "aria-live": "polite" });
      const logLink = h("a", { href: `#/sample/${sample.id12}/trust-qc`, onclick: async (event) => {
        event.preventDefault();
        logLine.replaceChildren("Loading run log line…");
        try {
          const line = await api(`/api/samples/${sample.id12}/run-log/${a.algorithm_id}`);
          logLine.replaceChildren(line.found
            ? `${line.run_id} · line ${line.line} · ${line.utc || "time unavailable"} · step_end ${line.step} status=${line.status}${line.skip_reason ? ` (${line.skip_reason})` : ""}${line.duration_s !== null ? ` · ${line.duration_s} s` : ""}`
            : "Run log line unavailable (searched up to 64 recently updated runs; logs over 1 MiB or 4,096 lines are not shown).");
        } catch (error) {
          logLine.replaceChildren(error.session ? "Session expired; reopen Lab." : "Run log line unavailable.");
        }
      } }, "Run log line →");
      cells.push(h("td", {}, logLink, " ", logLine));
      const detail = a.failure
        ? h("details", {}, h("summary", {}, "Last 50 stderr lines (scrubbed)"), h("pre", { class: "stderr" }, a.failure.stderr_tail.join("\n") || a.failure.first_line))
        : a.skip_message ? h("span", {}, a.skip_message) : "";
      cells.push(h("td", {}, detail));
      return h("tr", {}, ...cells);
    });
    out.push(h("section", { class: "panel" }, h("h2", {}, "Run"),
      pending ? h("p", { class: "status-line" }, `Latest run ${pending === active ? "in progress" : "interrupted"} during ${pending.step}; no result was published for this step.`) : "",
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Algorithm", "Status", "Duration", "Run log", "Detail"].map((t) => h("th", { scope: "col" }, t)))), h("tbody", {}, ...runRows)))));
    // QC bands.
    const banded = [];
    for (const a of sample.algorithms) for (const m of a.metrics) if (m.qc_band) banded.push([m.label, fmt(m.value), m.qc_band]);
    const glyph = { pass: "✓ pass", warn: "! warn", fail: "✕ fail" };
    out.push(h("section", { class: "panel" }, h("h2", {}, "QC bands"),
      h("p", { class: "help" }, "provisional (not from literature)"),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Metric"), h("th", {}, "Value"), h("th", {}, "Band"))),
        h("tbody", {}, ...banded.map(([label, value, band]) => h("tr", {}, h("th", { scope: "row" }, label), h("td", {}, value), h("td", {}, h("span", { class: "qc-band" }, glyph[band])))))))));
    const sex = sample.preanalytic_sex;
    let sexMessage;
    if (sex && sex.reason === "malformed_metadata") {
      sexMessage = "Stored operator sex assertion metadata is malformed; no comparison verdict. Measured results remain available.";
    } else if (!sex || sex.state === "no_verdict") {
      sexMessage = sex && sex.reason === "no_definite_call"
        ? "No definite measured chrX/chrY call is available; no comparison verdict."
        : "No operator sex assertion was supplied; no comparison was made.";
    } else if (sex.state === "mismatch") {
      sexMessage = `Pre-analytic mismatch: operator-declared ${sex.declared} differs from the measured chrX/chrY call (${sex.observed}). Review sample metadata and processing.`;
    } else if (sex.state === "concordant") {
      sexMessage = `Operator assertion (${sex.declared}) and measured chrX/chrY call (${sex.observed}) agree.`;
    } else {
      sexMessage = "Comparison state is unavailable; no comparison verdict.";
    }
    out.push(h("section", { class: "panel" }, h("h2", {}, "Pre-analytic sex assertion check"),
      h("p", {}, h("span", { class: "flag-pill" }, "FLAG: PRIVACY — can reveal pregnancy or transplant status.")),
      h("p", { class: sex && (sex.state === "mismatch" || sex.reason === "malformed_metadata") ? "problem" : "status-line" }, sexMessage),
      sex && sex.problem ? h("p", { class: "problem" }, `${sex.problem.code}: ${sex.problem.fix}`) : "",
      h("p", { class: "help" }, "This is not a diagnostic sex or gender determination.")));
    const health = sample.library_health;
    out.push(h("section", { class: "panel" }, h("h2", {}, "Library health"),
      h("p", {}, `no_library_entry: ${health.no_library_entry} · UNVERIFIED citations: ${health.unverified_citations} · interpretation v${health.version} · cohort ${sample.cohort_version || "none yet"} (members = ${sample.cohort_n})`),
      ["cohort_unavailable", "target_unavailable", "stale_target"].includes(sample.cohort_state)
        ? h("p", { class: "problem" }, `Cohort reference unavailable (${sample.cohort_state.replace(/_/g, " ")}); sample measurements remain visible without cohort states. ${sample.cohort_problem ? sample.cohort_problem.fix : "Check cohort build and reload."}`) : "",
      health.error ? h("div", { class: "problem" }, `Interpretation library on disk is invalid (${health.error}). Showing v${health.version}, loaded at ${health.loaded_at}.`) : ""));
    out.push(sizeDensity(sample));
    return out;
  }

  // -- Key dialog ---------------------------------------------------------------------
  function setupKey() {
    const dialog = document.getElementById("key-dialog");
    const button = document.getElementById("key-button");
    const rows = [
      ["Tier", "PROVEN (solid) · NEWER (outline) · SPECULATIVE (dashed outline)"],
      ["State", "in range ● · above typical ▲ · markedly above ▲▲ · below typical ▼ · markedly below ▼▼ · not interpretable ▨ · no reference ○"],
      ["Curve cell", "● usable · ◐ unstable · ▨ not interpretable · ○ skipped · ✕ failed · ◇ literature, not measured"],
      ["FLAG pill", "FLAG: TYPE — a regulatory, legal or privacy touchpoint, marked not designed around"],
      ["License", "Full callout with a red-orange border; later references are pills linking to it"],
      ["Citation", "UNVERIFIED citation: the numbers were not checked against the source"],
      ["Control", "SYNTHETIC (known answer) · REPEATABILITY (sampling noise) · KNOWN-STATUS (public sample)"],
      ["QC band", "✓ pass · ! warn · ✕ fail — provisional (not from literature)"],
    ];
    document.getElementById("key-body").replaceChildren(h("table", {}, h("tbody", {}, ...rows.map(([a, b]) => h("tr", {}, h("th", { scope: "row" }, a), h("td", {}, b))))));
    button.addEventListener("click", () => dialog.showModal());
    document.getElementById("key-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => button.focus());
  }

  // -- polling (D22: generation + heartbeat, never the lock) --------------------------------
  async function poll() {
    try {
      const status = await api("/api/status");
      state.missed = 0;
      state.lastOk = new Date();
      document.body.classList.remove("dimmed");
      const previous = state.lastStatus;
      state.lastStatus = status;
      const logIssue = status.run_log_unavailable;
      const logReason = logIssue && ({ invalid_run_id: "invalid run ID", unreadable_or_over_1mib: "log unreadable, unsafe, or over 1 MiB",
        over_4096_lines: "log over 4,096 lines", malformed_or_empty: "log malformed or empty" }[logIssue.reason] || "log unavailable");
      if (state.generationAtLoad === null) state.generationAtLoad = status.generation;
      if (status.active) {
        const minutes = Math.max(0, Math.round((Date.now() - Date.parse(status.active.started_utc)) / 60000));
        const priorInterrupted = status.interrupted && status.interrupted.run_id !== status.active.run_id
          ? ` Earlier run (${status.interrupted.run_id}) stopped during ${status.interrupted.step || "a step"}.` : "";
        const logNotice = logIssue ? ` Run log for ${logIssue.run_id} is unavailable (${logReason}).` : "";
        setStatus(`A Lab run is in progress (step: ${status.active.step || "starting"}, ${minutes} min). Results below are from completed steps.${priorInterrupted}${logNotice}`);
      } else if (status.interrupted) {
        // A run that died (stale heartbeat, open log) is "interrupted", even
        // if it published results before it stopped.
        setStatus(`The last run (${status.interrupted.run_id}) stopped during ${status.interrupted.step || "a step"}. Results shown are from completed steps. Rerun the same command; only missing results recompute.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (logIssue) {
        setStatus(`The last run (${logIssue.run_id}) status is unavailable (${logReason}). Results shown are from completed steps. Inspect the run log or rerun the command.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (status.generation > state.generationAtLoad && previous && previous.active) {
        setStatus("New results are available.", "Reload");
      } else if (status.generation > state.generationAtLoad) {
        setStatus("Results changed on disk since this page loaded.", "Reload");
      } else {
        setStatus("");
      }
      if (status.library && status.library.error) {
        document.getElementById("status").append(h("span", { class: "problem" },
          ` Interpretation library on disk is invalid (${status.library.error}). Showing v${status.library.version}, loaded at ${status.library.loaded_at}.`));
      }
      setControlsEnabled(true);
      const runMarker = (row) => row ? [row.run_id, row.step, row.source_id, row.input_id].join(":") : null;
      if (state.route.name === "lens" &&
          (runMarker(previous && previous.active) !== runMarker(status.active) ||
           runMarker(previous && previous.interrupted) !== runMarker(status.interrupted))) render();
    } catch (error) {
      if (error.session) { setStatus("Session expired. Restart `traceback lab serve --open`; it opens a fresh page."); return; }
      state.missed += 1;
      if (state.missed >= MISSED_LIMIT) {
        const since = state.lastOk ? state.lastOk.toTimeString().slice(0, 5) : "page load";
        setStatus(`Lab server not responding since ${since}. What you see may be out of date. Restart \`traceback lab serve --open\`; it opens a fresh page.`);
        document.body.classList.add("dimmed");
        setControlsEnabled(false);
      }
    }
  }

  // Disconnected: the page stays readable but every control that would fetch is off.
  function setControlsEnabled(enabled) {
    document.getElementById("sample-switcher").disabled = !enabled;
    for (const link of document.querySelectorAll("#lenses a, main a[href^='#/']")) {
      if (enabled) { link.removeAttribute("aria-disabled"); link.removeAttribute("tabindex"); link.classList.remove("disabled"); }
      else { link.setAttribute("aria-disabled", "true"); link.setAttribute("tabindex", "-1"); link.classList.add("disabled"); }
    }
    document.body.dataset.disconnected = enabled ? "0" : "1";
  }
  window.addEventListener("hashchange", (event) => {
    if (document.body.dataset.disconnected === "1") event.stopImmediatePropagation();
  }, true);

  // -- start ----------------------------------------------------------------------------
  async function start() {
    setupKey();
    render();
    try {
      await exchangeBootstrap();
      const listing = await api("/api/samples");
      state.samples = listing.samples;
      state.loading = false;
      await poll();
      await navigate();
    } catch (error) {
      state.loading = false;
      const main = document.getElementById("main");
      main.replaceChildren(h("h1", { class: "view-title" }, "Traceback Lab"),
        h("p", { class: "problem" }, error.session || error.message === "bootstrap"
          ? "This page needs the one-use link printed by `traceback lab serve`. Press Enter in that terminal for a fresh link."
          : `The Lab server could not be reached (${error.message}).`));
    }
    document.getElementById("sample-switcher").addEventListener("change", (event) => {
      location.hash = `#/?sample=${event.target.value}`;
    });
    window.addEventListener("hashchange", navigate);
    setInterval(poll, POLL_MS);
    document.body.dataset.ready = "1";
  }

  // Deferred scripts run while readyState is "interactive", before later
  // deferred scripts (including lab-views.js) have necessarily executed.
  // DOMContentLoaded waits for that whole ordered script set.
  if (document.readyState !== "complete") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
