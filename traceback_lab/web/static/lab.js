// Traceback Lab page.  Same-origin script only (CSP: script-src 'self', no
// inline script or style).  Text is set with textContent, never innerHTML.
// Research use only. Not CLIA-validated. Not a diagnosis.
"use strict";

(function () {
  // Record CSP violations first, so the headless CSP test can read them.
  const violations = [];
  document.body.dataset.cspViolations = "0";
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
    defaultSampleId12: null,
    completedInputUnavailable: false,
    completionHistoryUnavailable: false,
    sample: null,
    generationAtLoad: null,
    lastStatus: null,
    missed: 0,
    lastOk: null,
    route: { name: "home" },
    renderedHash: null,
    navigationRevision: 0,
    loading: true,
    runLogLines: new Map(),
    staleRunLogCleared: false,
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
    // Async route content may arrive after the disconnected state was set.
    if ((tag === "button" || tag === "select") && "data-requires-server" in (attrs || {})) {
      if (document.body.dataset.disconnected === "1") setFetchControlEnabled(el, false);
    }
    if (tag === "a" && typeof attrs?.href === "string" && attrs.href.startsWith("#/") &&
        document.body.dataset.disconnected === "1") setLinkEnabled(el, false);
    return el;
  }

  function setLinkEnabled(link, enabled) {
    if (enabled) { link.removeAttribute("aria-disabled"); link.removeAttribute("tabindex"); link.classList.remove("disabled"); }
    else { link.setAttribute("aria-disabled", "true"); link.setAttribute("tabindex", "-1"); link.classList.add("disabled"); }
  }

  function setFetchControlEnabled(control, enabled) {
    // A focused control keeps focus so the live status announcement does not
    // unexpectedly move a keyboard user. Capture listeners block its action.
    control.disabled = !enabled && document.activeElement !== control;
    if (enabled) {
      control.removeAttribute("aria-disabled");
      control.removeAttribute("aria-describedby");
      control.removeAttribute("tabindex");
      control.classList.remove("disabled");
    } else {
      control.setAttribute("aria-disabled", "true");
      control.setAttribute("aria-describedby", "status");
      control.setAttribute("tabindex", "-1");
      control.classList.add("disabled");
    }
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

  async function api(path, signal) {
    const response = await fetch(path, { credentials: "same-origin", ...(signal ? { signal } : {}) });
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
    if (!id12) return null;
    if (state.sample && state.sample.id12 === id12) return state.sample;
    const payload = await api(`/api/samples/${id12}`);
    return payload.found ? payload : { found: false, id12, candidates: payload.candidates || [] };
  }

  async function navigate() {
    const revision = ++state.navigationRevision;
    const requestedHash = location.hash || "#/";
    const route = parseRoute();
    const target = route.id12 || (route.name === "home" ? state.defaultSampleId12 : null);
    const sample = route.name === "home" || route.name === "lens" ? await loadSample(target) : state.sample;
    if (revision !== state.navigationRevision || (location.hash || "#/") !== requestedHash) return;
    if (sample !== state.sample) state.runLogLines.clear();
    state.sample = sample;
    state.route = route;
    render();
    state.renderedHash = requestedHash;
    const title = document.querySelector("main h1");
    if (title) { title.setAttribute("tabindex", "-1"); title.focus({ preventScroll: false }); }
    revealCurrentNav();
  }

  // -- render -------------------------------------------------------------------------
  function setStatus(text, action) {
    const status = document.getElementById("status");
    let message = status.querySelector("[data-status-message]");
    if (!message) {
      message = h("span", { "data-status-message": "" });
      status.prepend(message);
    }
    message.textContent = text || "";
    status.querySelector("[data-library-error]")?.remove();
    let reload = status.querySelector("button.reload");
    if (action) {
      if (!reload) {
        reload = h("button", { type: "button", class: "control reload", "data-requires-server": "", onclick: () => location.reload() }, action);
        status.append(h("span", { "data-reload-action": "" }, " ", reload));
      } else reload.textContent = action;
    } else if (reload) reload.closest("[data-reload-action]").remove();
  }

  function renderSwitcher() {
    const select = document.getElementById("sample-switcher");
    select.replaceChildren(
      ...(state.sample && state.sample.found ? [] : [h("option", { value: "", selected: true, disabled: true }, "Select a sample")]),
      ...state.samples.map((row) =>
        h("option", { value: row.id12, selected: state.sample && state.sample.id12 === row.id12 },
          `${row.id12} · ${row.run_utc ? row.run_utc.slice(0, 10) : "—"} · ${row.depth_x !== null ? row.depth_x.toFixed(2) + "x" : "—"}${row.example ? " · EXAMPLE (synthetic)" : ""}${row.state === "completed_input_unavailable" ? " · completed input unavailable" : row.state === "completion_history_unavailable" ? " · completion history unavailable" : ""}`)
      )
    );
  }

  function updateNavFade() {
    const nav = document.getElementById("lenses");
    const overflow = !nav.hidden && nav.scrollWidth > nav.clientWidth + 1;
    nav.classList.toggle("more-left", overflow && nav.scrollLeft > 1);
    nav.classList.toggle("more-right", overflow && nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 1);
  }

  function revealCurrentNav() {
    const nav = document.getElementById("lenses");
    const active = nav.querySelector('[aria-current="page"]');
    if (active && getComputedStyle(nav).flexWrap === "nowrap") {
      const navRect = nav.getBoundingClientRect();
      const activeRect = active.getBoundingClientRect();
      nav.scrollLeft += activeRect.left - navRect.left - (nav.clientWidth - activeRect.width) / 2;
    } else if (!nav.hidden) {
      nav.scrollLeft = 0;
    }
    updateNavFade();
  }

  function renderNav() {
    const nav = document.getElementById("lenses");
    const sample = state.sample && state.sample.found && !["completed_input_unavailable", "completion_history_unavailable"].includes(state.sample.state)
      ? state.sample : null;
    nav.replaceChildren();
    // Lens tabs belong to a sample's lens routes; the landing page has none (spec layout).
    nav.hidden = !sample || state.route.name !== "lens";
    if (!sample || nav.hidden) { updateNavFade(); return; }
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
    if (state.loading) {
      setStatus("Loading Lab results…");
      main.append(h("h1", { class: route.name === "home" ? "visually-hidden" : "view-title" }, "Loading…"));
      if (route.name === "home") {
        main.append(
          h("section", { class: "clia-panel loading-clia" },
            h("h2", { class: "clia-title" }, "What CLIA means for this demo"), h("p", {}, "Loading…")),
          h("p", { class: "headline loading-headline" }, "Loading…"),
          h("div", { class: "landing-band" },
            h("section", { class: "panel defended loading-defended" }, h("h2", {}, "Defended results"), h("p", {}, "Loading…")),
            h("section", { class: "panel curve loading-curve" }, h("h2", {}, "Depth → utility"), h("p", {}, "Loading…")),
            h("section", { class: "panel controls loading-controls" }, h("h2", {}, "Positive controls"), h("p", {}, "Loading…"))),
        );
      } else main.append(h("section", { class: "panel loading-lens" }, h("p", {}, "Loading…")));
      return;
    }
    if (route.name === "home" && !route.id12 && state.completionHistoryUnavailable) {
      main.append(h("h1", { class: "view-title" }, "Completion history unavailable"),
        window.TracebackLabViews.landingCliaPanel(),
        h("p", { class: "problem" }, "The latest recorded completion cannot be verified. Restore its run log or rerun the sample before viewing a default result."),
        h("ul", {}, ...state.samples.map((row) => h("li", {},
          h("a", { href: `#/?sample=${row.id12}` }, row.id12)))));
      return;
    }
    if (route.name === "home" && !route.id12 && state.completedInputUnavailable) {
      main.append(h("h1", { class: "view-title" }, "Latest completed input unavailable"),
        window.TracebackLabViews.landingCliaPanel(),
        h("p", { class: "problem" }, "The latest completed run's primary input cannot be read. Restore that input or rerun the sample."),
        h("ul", {}, ...state.samples.map((row) => h("li", {},
          h("a", { href: `#/?sample=${row.id12}` }, row.id12)))));
      return;
    }
    if (!state.samples.length && route.name === "home") {
      main.append(...emptyState());
      return;
    }
    if (route.name === "home" && !route.id12 && !state.defaultSampleId12) {
      main.append(h("h1", { class: "view-title" }, "No completed Lab run yet"),
        window.TracebackLabViews.landingCliaPanel(),
        h("p", { class: "empty" }, "The registered samples below have no completed analysis run. A running or interrupted analysis can be opened explicitly."),
        h("ul", {}, ...state.samples.map((row) => h("li", {},
          h("a", { href: `#/?sample=${row.id12}` }, row.id12)))));
      return;
    }
    if (route.name === "clia" && window.TracebackLabViews && window.TracebackLabViews.clia) { main.append(...window.TracebackLabViews.clia()); return; }
    const sample = state.sample;
    if (route.name === "unknown" || !sample || !sample.found) {
      const shown = route.id12 || (sample && sample.id12) || "";
      main.append(h("h1", { class: "view-title" }, "Unknown sample"));
      if (route.name === "home") main.append(window.TracebackLabViews.landingCliaPanel());
      main.append(h("p", {}, `No Lab sample matches ${shown}. `, h("a", { href: "#/" }, "Back to Lab home")));
      if (sample && sample.candidates && sample.candidates.length) main.append(h("p", {}, `Candidates: ${sample.candidates.join(", ")}`));
      return;
    }
    if (sample.state === "completed_input_unavailable") {
      main.append(h("h1", { class: "view-title" }, "Completed input unavailable"));
      if (route.name === "home") main.append(window.TracebackLabViews.landingCliaPanel());
      main.append(h("p", { class: "problem" }, `Sample ${sample.id12} exists, but its completed run's primary input cannot be read. Restore that input or rerun the sample.`),
        h("p", {}, h("a", { href: "#/" }, "Back to Lab home")));
      return;
    }
    if (sample.state === "completion_history_unavailable") {
      main.append(h("h1", { class: "view-title" }, "Completion history unavailable"));
      if (route.name === "home") main.append(window.TracebackLabViews.landingCliaPanel());
      main.append(h("p", { class: "problem" }, `Sample ${sample.id12} exists, but its recorded completion cannot be verified. Restore its run log or rerun the sample.`),
        h("p", {}, h("a", { href: "#/" }, "Back to Lab home")));
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
        const anchor = route.name === "home" ? main.querySelector(".clia-panel") : main.querySelector("h1");
        if (anchor) anchor.after(notice); else main.prepend(notice);
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
      window.TracebackLabViews.landingCliaPanel(),
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
    if (!algorithm || algorithm.status !== "ok") {
      const unreadable = !algorithm && (sample.unreadable_results || []).includes("fragment-size");
      section.append(h("p", { class: unreadable ? "problem" : "empty" }, unreadable
        ? "Chart could not be drawn (selected result unavailable)."
        : algorithm
          ? `fragment-size: ${algorithm.status}${algorithm.skip_reason ? ` (${algorithm.skip_reason.replace(/_/g, " ")})` : ""}.`
          : "fragment-size has no result for this sample."));
      return section;
    }
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
      const text = a.input_gate
        ? `${a.input_gate} → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status === "failed" ? "failed" : "ran"}`
        : a.min_depth_x
        ? `needs ${a.min_depth_x}x; this run ${depth !== null ? depth.toFixed(3) : "—"}x → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status === "failed" ? "failed" : "ran"}`
        : `any depth → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status}`;
      return h("tr", {}, h("th", { scope: "row" }, a.algorithm_id), h("td", {}, bar), h("td", {}, text));
    });
    out.push(h("section", { class: "panel" }, h("h2", {}, "Depth against each algorithm's minimum"),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Algorithm"), h("th", {}, "Depth"), h("th", {}, "Verdict"))), h("tbody", {}, ...rows)))));
    // Run table.
    const active = state.lastStatus && state.lastStatus.active;
    const interrupted = state.lastStatus && state.lastStatus.interrupted;
    const interruptedLabel = active && interrupted && active.run_id !== interrupted.run_id
      ? "earlier run interrupted" : "latest run interrupted";
    const unavailable = new Set([...(sample.unreadable_results || []), ...(sample.ambiguous_results || [])]);
    const pending = [active, interrupted].find((run) => run && run.step && !run.step.includes("@") && run.source_id === sample.source_id &&
      run.input_id === sample.full_input_id && !unavailable.has(run.step) &&
      !sample.algorithms.some((a) => a.algorithm_id === run.step));
    const runRows = sample.algorithms.map((a) => {
      const status = a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status;
      const matches = (run) => run && run.step === a.algorithm_id && run.source_id === sample.source_id && run.input_id === a.provenance?.input_id;
      const runNote = matches(active) ? "latest run in progress" : matches(interrupted) ? interruptedLabel : null;
      const duration = a.duration_s !== null && a.duration_s !== undefined ? `${a.duration_s.toFixed(1)} s` : "—";
      const cells = [h("th", { scope: "row" }, a.algorithm_id + (a.hidden ? " (intermediate)" : "")),
        h("td", { class: a.status === "failed" ? "problem" : null,
          "data-run-algorithm": a.algorithm_id, "data-run-input": a.provenance?.input_id || "" }, status, " ",
          h("span", { class: "state-chip run-note", "data-run-note": "", hidden: !runNote }, runNote || "")),
        h("td", { class: "num", "data-run-duration": duration }, runNote === "latest run in progress" && duration !== "—"
          ? `${duration} (selected result)` : duration)];
      const pin = a.provenance;
      const canPin = pin && /^[0-9a-f]{64}$/.test(pin.input_id) && /^[0-9a-f]{64}$/.test(pin.parameters_sha256) &&
        /^a-\d{8}T\d{12}-[0-9a-f]{8}$/.test(pin.attempt_id);
      const logKey = `${sample.id12}:${a.algorithm_id}:${pin?.input_id}:${pin?.parameters_sha256}:${pin?.attempt_id}`;
      const logLine = h("span", { class: "help", "aria-live": "polite", "data-run-log-line": "",
        "data-run-log-key": logKey }, state.runLogLines.get(logKey) || "");
      const showLogLine = (message) => {
        state.runLogLines.set(logKey, message);
        if (logLine.isConnected) logLine.replaceChildren(message);
        else if (state.route.name === "lens" && state.route.lens === "trust-qc" &&
                 state.sample && state.sample.id12 === sample.id12) {
          const current = [...document.querySelectorAll("[data-run-log-key]")]
            .find((node) => node.dataset.runLogKey === logKey);
          if (current) current.replaceChildren(message);
        }
      };
      const logLink = canPin ? h("button", { type: "button", class: "control",
        "data-requires-server": "",
        "aria-label": `Run log line for ${a.algorithm_id}`, onclick: async () => {
        if (state.lastStatus && state.generationAtLoad !== null && state.lastStatus.generation > state.generationAtLoad) {
          showLogLine("Results changed on disk; reload before viewing this run log line.");
          return;
        }
        showLogLine("Loading run log line…");
        try {
          const line = await api(`/api/samples/${sample.id12}/run-log/${a.algorithm_id}/${pin.input_id}/${pin.parameters_sha256}/${pin.attempt_id}`);
          showLogLine(line.found
            ? `${line.run_id} · line ${line.line} · ${line.utc || "time unavailable"} · step_end ${line.step} status=${line.status}${line.skip_reason ? ` (${line.skip_reason})` : ""}${line.duration_s !== null ? ` · ${line.duration_s} s` : ""}`
            : "Run log line unavailable. Results may have changed; reload this sample. Only 64 recent, bounded logs are searched.");
        } catch (error) {
          showLogLine(error.session ? "Session expired; reopen Lab." : "Run log line unavailable.");
        }
      } }, "Run log line →") : h("span", { class: "help" }, "Run log line unavailable for this result");
      cells.push(h("td", {}, logLink, " ", logLine));
      const detail = a.failure
        ? h("details", {}, h("summary", {}, "Last 50 stderr lines (scrubbed)"), h("pre", { class: "stderr" }, a.failure.stderr_tail.join("\n") || a.failure.first_line))
        : a.skip_message ? h("span", {}, a.skip_message) : "";
      cells.push(h("td", {}, detail));
      return h("tr", {}, ...cells);
    });
    for (const id of sample.unreadable_results || []) {
      runRows.push(h("tr", {}, h("th", { scope: "row" }, id), h("td", { class: "problem" }, "result unreadable"),
        h("td", {}, "—"), h("td", {}, "—"), h("td", {}, "A stored result could not be read; selection cannot be determined.")));
    }
    for (const id of sample.ambiguous_results || []) {
      runRows.push(h("tr", {}, h("th", { scope: "row" }, id), h("td", { class: "problem" }, "result selection ambiguous"),
        h("td", {}, "—"), h("td", {}, "—"), h("td", {}, "More than one selected result was found.")));
    }
    if (sample.malformed_result_selections) {
      runRows.push(h("tr", {}, h("th", { scope: "row" }, "Result"), h("td", { class: "problem" }, "malformed result selection"),
        h("td", {}, "—"), h("td", {}, "—"), h("td", {}, "A stored result selection could not be identified.")));
    }
    out.push(h("section", { class: "panel" }, h("h2", {}, "Run"),
      h("p", { class: "status-line", "data-run-pending": "", hidden: !pending }, pending
        ? `${pending === active ? "Latest run in progress" : interruptedLabel.replace(/^./, (letter) => letter.toUpperCase())} during ${pending.step || "a step"}; no result was published for this step.` : ""),
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
      if (!sex || sex.reason === "no_assertion") sexMessage = "No operator sex assertion was supplied; no comparison was made.";
      else if (sex.reason === "no_measurement") sexMessage = "No measured chrX/chrY call is available; no comparison verdict.";
      else if (sex.reason === "indeterminate_call") sexMessage = "Measured chrX/chrY call is indeterminate; no comparison verdict.";
      else sexMessage = "Measured chrX/chrY call is unrecognized; no comparison verdict.";
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
    const microbial = sample.microbial_profile;
    if (microbial) {
      out.push(h("section", { class: "panel" }, h("h2", {}, "Microbial candidates"),
        h("p", {}, h("span", { class: "flag-pill" }, "FLAG: PRIVACY — infection status is sensitive health data.")),
        h("p", { class: "help" }, "Exploratory Kraken2/Bracken assignments from human-unmapped reads. Not an infection diagnosis; residual human DNA and contamination may affect assignments."),
        microbial.database_source_label ? h("p", { class: "help" }, `Database: ${microbial.database_source_label} (SHA-256 ${microbial.database_sha256}). Fixed Bracken read length: ${microbial.read_length} bases.`) : "",
        h("p", { class: "help" }, "Viral taxa here are Bracken estimates per million QC-passing, non-duplicate primary reads. The separate viral-reference panel counts side-reference alignments per million human-unmapped primaries, including QC-fail and duplicate-flagged reads; the rates are not directly comparable."),
        microbial.reason ? h("p", { class: "problem" }, microbial.reason) : "",
        microbial.microbial_rpm_primary !== undefined
          ? h("p", {}, `Candidate microbial estimate: ${fmt(microbial.microbial_rpm_primary)} per million primary QC-passing reads; ${fmt(microbial.candidate_unmapped_reads)} unmapped reads submitted. Cohort 97.5th percentile: ${microbial.cohort_p975 === null ? "unavailable" : fmt(microbial.cohort_p975)}${microbial.cohort_p975 === null ? "" : ` (n=${microbial.cohort_n}, ${microbial.cohort_version})`}.`)
          : "",
        microbial.microbial_rpm_primary !== undefined
          ? h("p", { class: "help" }, `${fmt(microbial.too_short_unmapped_reads)} unmapped reads were shorter than the registered Bracken length; ${fmt(microbial.missing_sequence_unmapped_reads)} lacked sequence. Neither group was classified. The per-million rate still divides by all primary QC-passing non-duplicate reads.`)
          : "",
        microbial.omitted_taxa ? h("p", { class: "help" }, `${microbial.omitted_taxa} lower-abundance taxa omitted from the bounded display. The headline estimate includes them, so it can exceed the sum of visible rows; absent taxa are not assumed zero in cohort comparisons.`) : "",
        microbial.taxa.length
          ? h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, ...["Candidate taxon", "Group", "Estimate / million", "Cohort 97.5th pct", "Context"].map((label) => h("th", { scope: "col" }, label)))),
            h("tbody", {}, ...microbial.taxa.map((row) => h("tr", {},
              h("th", { scope: "row" }, `${row.name} (taxid ${row.taxid})`),
              h("td", {}, row.category), h("td", {}, fmt(row.rpm_primary)),
              h("td", {}, row.cohort_p975 === null ? `unavailable (n=${row.cohort_n})` : `${fmt(row.cohort_p975)} (n=${row.cohort_n})`),
              h("td", {}, row.above_cohort_p975 === null ? "no cohort reference" : row.above_cohort_p975 ? "above cohort percentile" : "at or below cohort percentile"))))))
          : h("p", {}, microbial.microbial_rpm_primary === 0
            ? "No species-level microbial estimate was returned under this database and read-length policy. Reads classified above species rank do not establish absence."
            : "No candidate microbial taxa are displayed in this profile.")));
    }
    const identityPairs = sample.identity_pairs || [];
    if (sample.algorithms.some((a) => a.algorithm_id === "identity") || identityPairs.length) {
      out.push(h("section", { class: "panel" }, h("h2", {}, "Identity comparisons"),
      h("p", {}, h("span", { class: "flag-pill" }, "FLAG: PRIVACY — pair comparison can reveal family relationships.")),
      h("p", { class: "help" }, "Exploratory Somalier measurements from explicitly selected stored samples. No ONT duplicate or kinship cutoff is claimed."),
      identityPairs.length
        ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {},
          ...["Other sample", "Relatedness", "Concordance", "Sites compared"].map((label) => h("th", { scope: "col" }, label)))),
          h("tbody", {}, ...identityPairs.map((pair) => h("tr", {},
            h("th", { scope: "row" }, pair.peer_id12),
            h("td", {}, pair.state === "unavailable" ? pair.reason : fmt(pair.metrics.relatedness)),
            h("td", {}, pair.state === "unavailable" ? "unavailable" : fmt(pair.metrics.concordance)),
            h("td", {}, pair.state === "unavailable" ? "unavailable" : fmt(pair.metrics.n)))))))
          : h("p", { class: sample.identity_pairs_reason ? "problem" : "" },
            sample.identity_pairs_reason || "No current pair comparison. Prepare fingerprints with lab run --only identity, then use lab identity compare SAMPLE_A SAMPLE_B.")));
    }
    const health = sample.library_health;
    out.push(h("section", { class: "panel" }, h("h2", {}, "Library health"),
      h("p", {}, `no_library_entry: ${health.no_library_entry} · UNVERIFIED citations: ${health.unverified_citations} · interpretation v${health.version} · cohort ${sample.cohort_version || "none yet"} (members = ${sample.cohort_n})`),
      ["cohort_unavailable", "target_unavailable", "stale_target"].includes(sample.cohort_state)
        ? h("p", { class: "problem" }, `Cohort reference unavailable (${sample.cohort_state.replace(/_/g, " ")}); sample measurements remain visible without cohort states. ${sample.cohort_problem ? sample.cohort_problem.fix : "Check cohort build and reload."}`) : "",
      sample.cohort_state === "ready" && sample.cohort_problem
        ? h("p", { class: "problem" }, `Panel of normals unavailable: copy-number and dependent cohort comparisons are withheld; independent metrics remain comparable. ${sample.cohort_problem.fix}`) : "",
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
      ["Curve cell", "● usable · ? stability not validated · ◐ unstable · ▨ not interpretable · ○ skipped · ✕ failed · ◇ literature, not measured"],
      ["FLAG pill", "FLAG: TYPE — a regulatory, legal or privacy touchpoint, marked not designed around"],
      ["License", "Full callout with a red-orange border; later references are pills linking to it"],
      ["Citation", "UNVERIFIED citation: the numbers were not checked against the source"],
      ["Control", "SYNTHETIC (known answer) · REPEATABILITY (sampling noise) · KNOWN-STATUS (public sample)"],
      ["QC band", "✓ pass · ! warn · ✕ fail — provisional (not from literature)"],
    ];
    document.getElementById("key-body").replaceChildren(h("table", {}, h("tbody", {}, ...rows.map(([a, b]) => h("tr", {}, h("th", { scope: "row" }, a), h("td", {}, b))))));
    button.addEventListener("click", () => dialog.showModal());
    document.getElementById("key-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      const active = document.activeElement;
      if (active === document.body || dialog.contains(active)) button.focus();
    });
  }

  // -- polling (D22: generation + heartbeat, never the lock) --------------------------------
  function updateTrustQcRunStatus(status) {
    if (state.route.name !== "lens" || state.route.lens !== "trust-qc" || !state.sample?.found) return;
    const sample = state.sample;
    const active = status.active;
    const interrupted = status.interrupted;
    const interruptedLabel = active && interrupted && active.run_id !== interrupted.run_id
      ? "earlier run interrupted" : "latest run interrupted";
    const matches = (run, algorithm, input) => run && run.step === algorithm &&
      run.source_id === sample.source_id && run.input_id === input;
    for (const cell of document.querySelectorAll("#main [data-run-algorithm]")) {
      const algorithm = cell.dataset.runAlgorithm;
      const input = cell.dataset.runInput;
      const note = matches(active, algorithm, input) ? "latest run in progress"
        : matches(interrupted, algorithm, input) ? interruptedLabel : "";
      const chip = cell.querySelector("[data-run-note]");
      chip.textContent = note;
      chip.hidden = !note;
      const duration = cell.nextElementSibling;
      const base = duration.dataset.runDuration;
      duration.textContent = note === "latest run in progress" && base !== "—"
        ? `${base} (selected result)` : base;
    }
    const unavailable = new Set([...(sample.unreadable_results || []), ...(sample.ambiguous_results || [])]);
    const pending = [active, interrupted].find((run) => run && run.step && !run.step.includes("@") && run.source_id === sample.source_id &&
      run.input_id === sample.full_input_id && !unavailable.has(run.step) &&
      !sample.algorithms.some((algorithm) => algorithm.algorithm_id === run.step));
    const pendingLine = document.querySelector("#main [data-run-pending]");
    if (pendingLine) {
      pendingLine.textContent = pending
        ? `${pending === active ? "Latest run in progress" : interruptedLabel.replace(/^./, (letter) => letter.toUpperCase())} during ${pending.step || "a step"}; no result was published for this step.` : "";
      pendingLine.hidden = !pending;
    }
  }

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    const controller = new AbortController();
    let timeoutId;
    // Finish one observation before the next 15-second poll is due.
    const deadline = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        controller.abort();
        reject(new Error("status poll deadline"));
      }, Math.max(100, POLL_MS * 0.8));
    });
    try {
      const status = await Promise.race([api("/api/status", controller.signal), deadline]);
      state.missed = 0;
      state.lastOk = new Date();
      document.body.classList.remove("dimmed");
      const previous = state.lastStatus;
      state.lastStatus = status;
      const logIssue = status.run_log_unavailable;
      const logReason = logIssue && ({ invalid_run_id: "invalid run ID", missing: "log missing", unreadable_or_over_1mib: "log unreadable, unsafe, or over 1 MiB",
        over_4096_lines: "log over 4,096 lines", malformed_or_empty: "log malformed or empty",
        latest_run_pointer_unavailable: "latest-run pointer unavailable" }[logIssue.reason] || "log unavailable");
      if (state.generationAtLoad === null) state.generationAtLoad = status.generation;
      if (status.generation > state.generationAtLoad && !state.staleRunLogCleared) {
        state.runLogLines.clear();
        document.querySelectorAll("[data-run-log-line]").forEach((node) => node.replaceChildren());
        state.staleRunLogCleared = true;
      }
      if (status.active) {
        const minutes = Math.max(0, Math.round((Date.now() - Date.parse(status.active.started_utc)) / 60000));
        const priorInterrupted = status.interrupted && status.interrupted.run_id !== status.active.run_id
          ? ` Earlier run (${status.interrupted.run_id}) stopped during ${status.interrupted.step || "a step"}.` : "";
        const logNotice = logIssue ? ` ${logIssue.run_id ? `Run log for ${logIssue.run_id}` : "Run status"} is unavailable (${logReason}).` : "";
        if (["run", "simulate cna", "simulate split"].includes(status.active.command)) {
          setStatus(`A Lab run is in progress (step: ${status.active.step || "starting"}, ${minutes} min). Results below are from completed steps.${priorInterrupted}${logNotice}`,
            status.generation > state.generationAtLoad ? "Reload" : null);
        } else {
          setStatus(`Lab ${status.active.command || "work"} is in progress. Results below are from completed steps.${priorInterrupted}${logNotice}`,
            status.generation > state.generationAtLoad ? "Reload" : null);
        }
      } else if (status.interrupted) {
        // A run that died (stale heartbeat, open log) is "interrupted", even
        // if it published results before it stopped.
        setStatus(`The last run (${status.interrupted.run_id}) stopped during ${status.interrupted.step || "a step"}. Results shown are from completed steps. Rerun the same command; only missing results recompute.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (logIssue) {
        setStatus(`The last run${logIssue.run_id ? ` (${logIssue.run_id})` : ""} status is unavailable (${logReason}). Results shown are from completed steps. Inspect the run log or rerun the command.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (status.generation > state.generationAtLoad && previous && previous.active) {
        setStatus("New results are available.", "Reload");
      } else if (status.generation > state.generationAtLoad) {
        setStatus("Results changed on disk since this page loaded.", "Reload");
      } else {
        setStatus("");
      }
      if (status.library && status.library.error) {
        document.getElementById("status").append(h("span", { class: "problem", "data-library-error": "" },
          ` Interpretation library on disk is invalid (${status.library.error}). Showing v${status.library.version}, loaded at ${status.library.loaded_at}.`));
      }
      setControlsEnabled(true);
      updateTrustQcRunStatus(status);
    } catch (error) {
      if (error.session) { setStatus("Session expired. Restart `traceback lab serve --open`; it opens a fresh page."); return; }
      state.missed += 1;
      if (state.missed >= MISSED_LIMIT) {
        const since = state.lastOk ? state.lastOk.toTimeString().slice(0, 5) : "page load";
        setStatus(`Lab server not responding since ${since}. What you see may be out of date. Restart \`traceback lab serve --open\`; it opens a fresh page.`);
        document.body.classList.add("dimmed");
        restoreRenderedRoute();
        setControlsEnabled(false);
      }
    } finally {
      clearTimeout(timeoutId);
      polling = false;
    }
  }

  // Disconnected: the page stays readable but every control that would fetch is off.
  function setControlsEnabled(enabled) {
    setFetchControlEnabled(document.getElementById("sample-switcher"), enabled);
    for (const control of document.querySelectorAll("button[data-requires-server], select[data-requires-server]")) {
      setFetchControlEnabled(control, enabled);
    }
    for (const link of document.querySelectorAll("a[href^='#/']")) setLinkEnabled(link, enabled);
    document.body.dataset.disconnected = enabled ? "0" : "1";
  }
  function restoreRenderedRoute() {
    if (!state.renderedHash || location.hash === state.renderedHash) return;
    // Cancel a sample fetch that started before disconnection. Its late answer
    // must not replace the route the reader was actually viewing.
    state.navigationRevision += 1;
    history.replaceState(null, "", location.pathname + location.search + state.renderedHash);
  }
  document.addEventListener("click", (event) => {
    if (document.body.dataset.disconnected !== "1" || !(event.target instanceof Element) ||
        !event.target.closest("a[href^='#/'], [data-requires-server], #sample-switcher")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.addEventListener("change", (event) => {
    if (document.body.dataset.disconnected !== "1" || !(event.target instanceof Element) ||
        !event.target.matches("select[data-requires-server], #sample-switcher")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  for (const type of ["keydown", "pointerdown"]) {
    document.addEventListener(type, (event) => {
      if (document.body.dataset.disconnected !== "1" || !(event.target instanceof Element) ||
          !event.target.matches("select[data-requires-server], #sample-switcher")) return;
      // Keep keyboard escape and focus travel available to local Key and
      // disclosures; every selection-changing key remains blocked.
      if (type === "keydown" && ["Tab", "Escape", "F6"].includes(event.key)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
  }
  window.addEventListener("hashchange", (event) => {
    if (document.body.dataset.disconnected !== "1") return;
    event.stopImmediatePropagation();
    restoreRenderedRoute();
  }, true);

  // -- start ----------------------------------------------------------------------------
  async function start() {
    setupKey();
    document.getElementById("lenses").addEventListener("scroll", updateNavFade, { passive: true });
    window.addEventListener("resize", revealCurrentNav);
    render();
    try {
      await exchangeBootstrap();
      state.route = parseRoute();
      render();
      const baselineController = new AbortController();
      const baselineDeadline = setTimeout(() => baselineController.abort(), Math.max(100, POLL_MS * 0.8));
      try {
        const baseline = await api("/api/status", baselineController.signal);
        state.generationAtLoad = baseline.generation;
        state.lastStatus = baseline;
      } catch (error) {
        if (error.session) throw error;
      } finally {
        clearTimeout(baselineDeadline);
      }
      const listing = await api("/api/samples");
      state.samples = listing.samples;
      state.defaultSampleId12 = listing.default_sample_id12 !== undefined
        ? listing.default_sample_id12 : (listing.samples[0] && listing.samples[0].id12) || null;
      state.completedInputUnavailable = !!listing.completed_input_unavailable;
      state.completionHistoryUnavailable = !!listing.completion_history_unavailable;
      state.loading = false;
      setStatus("");
      await navigate();
      await poll();
    } catch (error) {
      state.loading = false;
      setStatus("");
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
