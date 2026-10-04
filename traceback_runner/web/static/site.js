// The local records site (usability C0, C1, C3, C5): hash routes over the
// operator-only record routes.
//
//   #/                      catalog: one row per signed local record (home)
//   #/records/{record_id}   one record: status, histogram, denominator, states
//   #/compare?a=..&b=..     hook for pair comparison (C6, not built yet)
//
// It starts only when app.js has bound an operator session (the
// "traceback:operator" event).  Every view sets data-view and data-state
// (loading | empty | error | success | partial) on #site, as the E12 view does.
// It refreshes on window focus and on the Refresh button; it never polls.
// Values are written with textContent only; no style attributes (CSP).
//
// Every record here is unqualified, local and not for clinical use.
(() => {
  "use strict";
  const doc = document;
  const byId = (id) => doc.getElementById(id);
  const chart = window.TracebackChart;
  const SELECTION_KEY = "traceback.compare-selection";
  const RECORD_ROUTE = /^#\/records\/(record-[0-9a-f]{24})$/;
  const COMPARE_ROUTE = /^#\/compare\?(.*)$/;
  const RECORD_ID = /^record-[0-9a-f]{24}$/;
  const AXIS_NAMES = {
    qualification: "Qualification",
    trust: "Trust",
    display_role: "Display role",
    reference_match: "Reference match",
    preflight: "Preflight",
    comparison: "Comparison",
  };
  const RUNNING_STATES = new Set([
    "discovered", "waiting_for_finalization", "snapshotting", "validating", "ready", "queued",
    "running", "basecalling", "aligning", "sorting_indexing", "technical_qc", "measuring",
    "pause_requested", "validating_output", "signing",
  ]);

  let started = false;
  let inflight = 0;
  let records = null; // last record list payload
  let jobs = null; // last jobs payload, or "unavailable"
  let currentRecord = null; // last record view payload
  let lastRoute = null;
  // Each render takes a generation; a response for an older one is dropped,
  // so a slow earlier route can never overwrite the current one.
  let generation = 0;

  const el = (tag, text, attrs) => {
    const node = doc.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    Object.entries(attrs || {}).forEach(([name, value]) => node.setAttribute(name, String(value)));
    return node;
  };
  const link = (href, text) => el("a", text, { href });
  const count = (value) => chart.count(value);
  const share = (numerator, denominator) => chart.share(numerator, denominator);
  const when = (iso) => (iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC` : "unknown");
  const recordName = (item) => item.label || item.short_id;

  // --- selection (sessionStorage; absent storage just means no memory) -------
  const loadSelection = () => {
    try {
      const raw = window.sessionStorage.getItem(SELECTION_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(parsed) ? parsed.filter((id) => RECORD_ID.test(id)) : []);
    } catch (_) {
      return new Set();
    }
  };
  let selection = loadSelection();
  const saveSelection = () => {
    try { window.sessionStorage.setItem(SELECTION_KEY, JSON.stringify([...selection])); } catch (_) { /* no storage */ }
  };
  const clearSelection = () => {
    selection = new Set();
    try { window.sessionStorage.removeItem(SELECTION_KEY); } catch (_) { /* no storage */ }
  };

  // --- shell -------------------------------------------------------------------
  const site = () => byId("site");
  const view = () => byId("site-view");
  const live = (text) => { byId("site-live").textContent = text; };
  const setState = (name, state) => {
    site().dataset.view = name;
    site().dataset.state = state;
    site().setAttribute("aria-busy", state === "loading" ? "true" : "false");
    doc.documentElement.dataset.viewState = `${name}:${state}`;
  };
  const heading = (text) => el("h1", text, { id: "view-title", tabindex: "-1", class: "view-title" });
  const focusHeading = () => {
    const title = byId("view-title");
    if (title && typeof title.focus === "function") title.focus();
  };

  const getJson = async (path) => {
    inflight += 1;
    try {
      const response = await window.fetch(path, { credentials: "same-origin" });
      let payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      return { status: response.status, payload };
    } catch (_) {
      return { status: 0, payload: null };
    } finally {
      inflight -= 1;
    }
  };

  const sessionEnded = (name) => {
    clearSelection();
    setState(name, "error");
    const body = view();
    body.replaceChildren(
      heading("Session ended"),
      el("p", "Session ended. Run traceback serve again and open its new link.", { class: "problem" }),
    );
    live("Session ended");
  };

  const problemBox = (title, text, retry) => {
    const box = el("div", null, { class: "problem", role: "alert" });
    box.append(el("p", title, { class: "problem-title" }), el("p", text));
    if (retry) {
      const button = el("button", "Retry", { type: "button", class: "control" });
      button.addEventListener("click", retry);
      box.append(button);
    }
    return box;
  };

  // --- jobs disclosure (one line) ----------------------------------------------
  const jobCopy = (token) => {
    const row = records && records.job_states ? records.job_states.find((item) => item.token === token) : null;
    return row ? row.label : "Unrecognised job state";
  };
  const ago = (iso) => {
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) return `${seconds} s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ${seconds % 60} s ago`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60} min ago`;
  };
  const renderJobs = () => {
    const summary = byId("jobs-summary");
    const list = byId("jobs");
    if (!summary || !list) return;
    list.replaceChildren();
    if (jobs === null) {
      summary.textContent = "Jobs: loading";
      return;
    }
    if (jobs === "unavailable" || !Array.isArray(jobs.jobs)) {
      summary.textContent = "Job status unavailable";
      return;
    }
    const items = jobs.jobs;
    if (!items.length) {
      summary.textContent = "No job has run on this ROOT";
      return;
    }
    const running = items.filter((job) => RUNNING_STATES.has(job.state));
    if (running.length) {
      const job = running[0];
      const stale = job.stale ? "; status may be out of date" : "";
      summary.textContent = `Running: ${jobCopy(job.state)}, stage ${job.stage_label} (last update ${ago(job.updated_at)})${stale}`;
    } else {
      const last = items[0];
      summary.textContent = last.state === "complete"
        ? "Last job finished (signed record written); no job is running"
        : `Last job: ${jobCopy(last.state)}; no job is running`;
    }
    items.forEach((job) => {
      const stale = job.stale && RUNNING_STATES.has(job.state) ? "; status may be out of date" : "";
      list.append(el("li", `${jobCopy(job.state)}; stage ${job.stage_label}; updated ${when(job.updated_at)}${stale}`));
    });
  };
  const loadJobs = async () => {
    const { status, payload } = await getJson("/api/v1/jobs");
    jobs = status === 200 && payload ? payload : "unavailable";
    renderJobs();
  };

  // --- catalog ----------------------------------------------------------------
  const verifyCommand = (recordId) => `traceback verify ROOT/records/${recordId} --trust-store ROOT/trust/development-result-trust.json`;

  const compareControls = () => {
    const toolbar = el("div", null, { class: "toolbar" });
    const refresh = el("button", "Refresh", { type: "button", id: "refresh", class: "control" });
    refresh.addEventListener("click", () => render({ focus: false }));
    const compare = el("button", "Compare selected", {
      type: "button", id: "compare", class: "control", "aria-describedby": "compare-reason",
    });
    const reason = el("span", "", { id: "compare-reason", role: "status", "aria-live": "polite", class: "help" });
    compare.addEventListener("click", () => {
      const verified = (records ? records.records : []).filter((item) => item.status === "verified" && selection.has(item.record_id));
      if (verified.length !== 2) return;
      const [a, b] = verified; // list order is import order, so A is the earlier import
      window.location.hash = `#/compare?a=${a.record_id}&b=${b.record_id}`;
    });
    toolbar.append(refresh, compare, reason);
    return toolbar;
  };
  const updateCompare = () => {
    const button = byId("compare");
    const reason = byId("compare-reason");
    if (!button || !reason) return;
    const known = new Set((records ? records.records : []).filter((item) => item.status === "verified").map((item) => item.record_id));
    const chosen = [...selection].filter((id) => known.has(id)).length;
    button.disabled = chosen !== 2;
    if (chosen === 2) button.removeAttribute("aria-disabled");
    reason.textContent = chosen === 2
      ? "2 records selected; ready to open them together"
      : `Select exactly 2 records (${chosen} selected)`;
  };

  const filterSelect = (id, label, values) => {
    const wrapper = el("label", label, { class: "filter" });
    const select = el("select", null, { id });
    const all = el("option", label === "Method version" ? "All method versions" : "All policies", { value: "" });
    select.append(all, ...values.map((value) => el("option", value, { value })));
    wrapper.append(select);
    return { wrapper, select };
  };

  const recordRow = (item) => {
    const tr = el("tr", null, { "data-record": item.record_id, "data-status": item.status });
    const pick = el("td", null, { class: "pick", "data-label": "Compare" });
    const box = el("input", null, {
      type: "checkbox",
      class: "pick-box",
      "aria-label": `Compare ${recordName(item)}`,
      "data-record": item.record_id,
    });
    box.checked = selection.has(item.record_id);
    if (item.status !== "verified") {
      box.disabled = true;
      box.checked = false;
    }
    box.addEventListener("change", () => {
      if (box.checked) selection.add(item.record_id); else selection.delete(item.record_id);
      saveSelection();
      updateCompare();
    });
    const pickLabel = el("label", null, { class: "pick-target" });
    pickLabel.append(box);
    pick.append(pickLabel);
    tr.append(pick);
    const name = el("th", null, { scope: "row", "data-label": "Record" });
    if (item.status === "verified") {
      name.append(link(`#/records/${item.record_id}`, recordName(item)));
    } else {
      name.append(el("span", recordName(item)));
    }
    if (item.label) name.append(el("span", ` ${item.short_id}`, { class: "short-id" }));
    tr.append(name);
    if (item.status !== "verified") {
      const cell = el("td", null, { colspan: "6", class: "row-problem", "data-label": "Status" });
      cell.append(el("strong", item.status_label));
      cell.append(el("span", item.status === "failed_verification"
        ? ". Nothing from this record is shown. Check it with: "
        : ". Import the record again with: "));
      cell.append(el("code", item.status === "failed_verification"
        ? verifyCommand(item.record_id)
        : `traceback catalog import ROOT/records/${item.record_id} --root ROOT`));
      tr.append(cell);
      return tr;
    }
    const cells = [
      ["Reference", item.reference_id, ""],
      ["Policy", item.policy_label, ""],
      ["Eligible alignments", count(item.eligible_alignments), "num"],
      ["Records scanned", count(item.records_scanned), "num"],
      ["Preflight", item.preflight_warnings
        ? `${item.preflight_label}; ${item.preflight_warnings} warning${item.preflight_warnings === 1 ? "" : "s"}`
        : item.preflight_label, ""],
      ["Imported", when(item.imported_at), ""],
    ];
    cells.forEach(([label, value, cls]) => {
      const cell = el("td", null, { "data-label": label, class: cls });
      if (label === "Preflight" && item.warning_texts && item.warning_texts.length) {
        const details = el("details", null, { class: "row-warnings" });
        details.append(el("summary", value));
        const list = el("ul");
        item.warning_texts.forEach((text) => list.append(el("li", text)));
        details.append(list);
        cell.append(details);
      } else {
        cell.textContent = value;
      }
      tr.append(cell);
    });
    return tr;
  };

  const renderCatalog = async (focus) => {
    if (focus) showLoading("catalog", "Loading records…");
    else setState("catalog", "loading");
    currentRecord = null;
    const mine = generation;
    const { status, payload } = await getJson("/api/v1/records");
    if (mine !== generation) return;
    if (status === 401) { sessionEnded("catalog"); return; }
    const body = view();
    if (status !== 200 || !payload || !Array.isArray(payload.records)) {
      setState("catalog", "error");
      const code = payload && payload.error && payload.error.code ? payload.error.code : `HTTP ${status}`;
      body.replaceChildren(
        heading("Records"),
        problemBox(`Could not read the catalog (${code}).`, "Run traceback doctor.", () => render({ focus: false })),
      );
      live("Could not read the catalog");
      if (focus) focusHeading();
      return;
    }
    records = payload;
    renderJobs();
    const items = payload.records;
    const known = new Set(items.filter((item) => item.status === "verified").map((item) => item.record_id));
    selection = new Set([...selection].filter((id) => known.has(id)));
    saveSelection();
    const nodes = [heading("Records")];
    nodes.push(el("p", "Signed local records in this ROOT, oldest import first. A label is an operator note, not part of the signed record.", { class: "help" }));
    if (!items.length) {
      setState("catalog", "empty");
      nodes.push(el("p", "No records yet. Run: traceback run BAM --reference ID --label NAME --import", { class: "empty" }));
      body.replaceChildren(...nodes);
      live("No records yet");
      if (focus) focusHeading();
      return;
    }
    const verified = items.filter((item) => item.status === "verified");
    nodes.push(compareControls());
    const versions = [...new Set(verified.map((item) => item.method_version))].sort();
    const policies = [...new Set(verified.map((item) => item.policy_label))].sort();
    const filters = el("div", null, { class: "filters" });
    const reference = filterSelect("filter-method", "Method version", versions);
    const policy = filterSelect("filter-policy", "Analysis policy", policies);
    filters.append(reference.wrapper, policy.wrapper);
    nodes.push(filters);
    const wrap = el("div", null, { class: "table-wrap", role: "region", "aria-labelledby": "records-caption", tabindex: "0" });
    const table = el("table", null, { class: "records" });
    table.append(el("caption", `${items.length} record${items.length === 1 ? "" : "s"}`, { id: "records-caption" }));
    const head = el("thead");
    const headRow = el("tr");
    ["Compare", "Record (operator note)", "Reference", "Policy", "Eligible alignments", "Records scanned", "Preflight", "Imported"]
      .forEach((text) => headRow.append(el("th", text, { scope: "col" })));
    head.append(headRow);
    const tbody = el("tbody");
    const rows = items.map((item) => [item, recordRow(item)]);
    rows.forEach(([, row]) => tbody.append(row));
    table.append(head, tbody);
    wrap.append(table);
    nodes.push(wrap);
    if (payload.truncated) nodes.push(el("p", "Only the first 500 records are listed.", { class: "help" }));
    const applyFilters = () => {
      rows.forEach(([item, row]) => {
        row.hidden = Boolean(
          (reference.select.value && item.method_version !== reference.select.value)
          || (policy.select.value && item.policy_label !== policy.select.value),
        );
      });
    };
    reference.select.addEventListener("change", applyFilters);
    policy.select.addEventListener("change", applyFilters);
    body.replaceChildren(...nodes);
    updateCompare();
    const failed = items.length - verified.length;
    setState("catalog", failed ? "partial" : "success");
    live(failed
      ? `${items.length} records; ${failed} could not be shown`
      : `${items.length} record${items.length === 1 ? "" : "s"} loaded`);
    if (focus) focusHeading();
  };

  // --- record -----------------------------------------------------------------
  const stateRow = (record, axis) => record.states.find((item) => item.axis === axis);
  const compactQuery = typeof window.matchMedia === "function" ? window.matchMedia("(max-width: 42rem)") : null;
  const isCompact = () => Boolean(compactQuery && compactQuery.matches);

  const sumWhere = (record, test) => record.histogram.filter(test).reduce((total, row) => total + row.count, 0);
  const hasEdge = (record, edge) => record.histogram.some((row) => row.lower === edge);
  const share150to200 = (record) => (hasEdge(record, 150) && hasEdge(record, 200)
    ? share(sumWhere(record, (row) => row.lower >= 150 && row.upper !== null && row.upper <= 200), record.eligible_alignments)
    : null);
  const shareOver1kb = (record) => (hasEdge(record, 1000)
    ? share(sumWhere(record, (row) => row.lower >= 1000), record.eligible_alignments)
    : null);

  const policyText = (record) => (record.policy.builtin
    ? `built-in (MAPQ ≥ ${record.policy.min_mapq})`
    : `${record.policy.id}${record.policy.min_mapq === null || record.policy.min_mapq === undefined ? "" : ` (MAPQ ≥ ${record.policy.min_mapq})`}`);

  const warningItems = (record) => {
    const items = record.preflight.checks.map((check) => `${check.code}: ${check.summary || "see the operator guide for this code"}`);
    if (record.preflight.origin !== "job_store" && stateRow(record, "reference_match").token === "name_and_length_only") {
      items.push("Reference matched by contig name and length only (stated in the signed record)");
    }
    return items;
  };

  const denominator = (record) => {
    const section = el("section", null, { class: "panel", "aria-labelledby": "denominator-title", id: "denominator" });
    section.append(el("h2", "Denominator", { id: "denominator-title" }));
    const values = el("dl", null, { class: "strip-values" });
    const pair = (term, value) => values.append(el("dt", term), el("dd", value));
    pair("Eligible of scanned", `${count(record.eligible_alignments)} of ${count(record.records_scanned)} (${share(record.eligible_alignments, record.records_scanned)})`);
    pair("Share over 1 kb", shareOver1kb(record) || "not available for this policy's bins");
    pair("Share 150 to 199 bp", share150to200(record) || "not available for this policy's bins");
    section.append(values);
    // One horizontal bar: every scanned record, split into exclusions and eligible.
    const ns = ["http", "://www.w3.org/2000/svg"].join("");
    const svgNode = doc.createElementNS(ns, "svg");
    svgNode.setAttribute("viewBox", "0 0 1000 28");
    svgNode.setAttribute("class", "strip-bar");
    svgNode.setAttribute("role", "img");
    svgNode.setAttribute("aria-labelledby", "strip-title");
    const title = doc.createElementNS(ns, "title");
    title.setAttribute("id", "strip-title");
    title.textContent = `Of ${count(record.records_scanned)} records scanned, ${count(record.eligible_alignments)} are eligible; the list below gives each exclusion.`;
    svgNode.append(title);
    let x = 0;
    const segments = [...record.exclusions.filter((row) => row.count > 0).map((row, index) => ({ count: row.count, cls: index % 2 ? "seg seg-excluded-alt" : "seg seg-excluded" })),
      { count: record.eligible_alignments, cls: "seg seg-eligible" }];
    segments.forEach((segment) => {
      const width = record.records_scanned ? (segment.count / record.records_scanned) * 1000 : 0;
      const rect = doc.createElementNS(ns, "rect");
      rect.setAttribute("x", x.toFixed(2));
      rect.setAttribute("y", "0");
      rect.setAttribute("width", Math.max(width, 0).toFixed(2));
      rect.setAttribute("height", "28");
      rect.setAttribute("class", segment.cls);
      svgNode.append(rect);
      x += width;
    });
    section.append(svgNode);
    const list = el("ol", null, { class: "strip-list" });
    list.append(el("li", `Records scanned: ${count(record.records_scanned)}`, { "data-strip": "scanned", "data-count": record.records_scanned }));
    record.exclusions.forEach((row) => {
      list.append(el("li", `minus ${row.label}: ${count(row.count)} (${share(row.count, record.records_scanned)} of scanned)`,
        { "data-strip": "excluded", "data-count": row.count }));
    });
    list.append(el("li", `equals Eligible alignments: ${count(record.eligible_alignments)} (${share(record.eligible_alignments, record.records_scanned)} of scanned)`,
      { "data-strip": "eligible", "data-count": record.eligible_alignments }));
    section.append(list);
    return section;
  };

  const whatItIs = (record) => {
    const section = el("section", null, { class: "panel", "aria-labelledby": "states-title", id: "states" });
    section.append(el("h2", "What this record is", { id: "states-title" }));
    const table = el("table", null, { class: "states" });
    table.append(el("caption", "One row per state, in plain words"));
    const tbody = el("tbody");
    record.states.forEach((row) => {
      const tr = el("tr", null, { "data-axis": row.axis });
      tr.append(el("th", AXIS_NAMES[row.axis] || "State", { scope: "row" }));
      const cell = el("td");
      cell.append(el("strong", row.label), el("span", ` ${row.meaning}`));
      if (row.axis === "preflight") {
        const warnings = warningItems(record);
        if (warnings.length) {
          const list = el("ul", null, { class: "warnings" });
          warnings.forEach((text) => list.append(el("li", text)));
          cell.append(list);
        }
      }
      tr.append(cell);
      tbody.append(tr);
    });
    table.append(tbody);
    section.append(table);
    return section;
  };

  const howToRead = (record) => {
    const section = el("section", null, { class: "panel", "aria-labelledby": "read-title", id: "how-to-read" });
    section.append(el("h2", "How to read it (descriptive, not diagnostic)", { id: "read-title" }));
    section.append(el("p", "Each bar covers one bin of aligned reference span. Its area is the share of eligible alignments in that bin, so its height is that share divided by the bin width in bp. Wide bins are therefore not drawn taller just for being wide."));
    const central = share150to200(record);
    if (central) {
      section.append(el("p", `In this record, ${central} of eligible alignments have an aligned reference span of 150 to 199 bp.`, { "data-descriptive": "share-150-200" }));
    }
    section.append(el("p", "These are counts from one local run of one BAM under one analysis policy. They describe this record only. There is no reference range, threshold or comparison population on this page, and no value here is a diagnosis or a screening result."));
    return section;
  };

  const exactValues = (record) => {
    const details = el("details", null, { class: "exact", id: "exact-values" });
    details.append(el("summary", "Exact values and identities"));
    const list = el("dl");
    const pair = (term, value) => list.append(el("dt", term), el("dd", value));
    pair("Record ID", record.record_id);
    pair("Result ID", record.result_id);
    pair("Measurement SHA-256", record.measurement_sha256);
    pair("Method version", record.method_version);
    pair("Policy", `${record.policy.id}; minimum MAPQ ${record.policy.min_mapq === null || record.policy.min_mapq === undefined ? "not recorded" : record.policy.min_mapq}; bin edges ${record.policy.bins.map((bin) => bin.lower).join(", ")} and over`);
    pair("Imported", when(record.imported_at));
    pair("Preflight source", record.preflight.origin === "job_store" ? "the run's preflight report in ROOT's job store (not signed)" : "not available");
    record.states.forEach((row) => pair(`${AXIS_NAMES[row.axis] || row.axis} token`, row.token));
    pair("Signed report", `ROOT/records/${record.record_id}/report.html is the record of truth`);
    pair("Label", record.label ? `${record.label} (unsigned operator note in ROOT/labels)` : "none");
    details.append(list);
    return details;
  };

  const chartFigure = (record) => {
    const figure = el("figure", null, { class: "panel chart", id: "histogram" });
    if (!record.eligible_alignments) {
      figure.append(el("p", "No eligible alignments; see the denominator strip.", { class: "empty" }));
      return figure;
    }
    const data = { rows: record.histogram, eligible: record.eligible_alignments, compact: isCompact(), idPrefix: "hist" };
    const drawn = chart.histogramSvg(doc, data);
    figure.append(drawn.root);
    const caption = el("figcaption", `n = ${count(record.eligible_alignments)} eligible alignments; policy ${policyText(record)}.`, { class: "chart-caption" });
    figure.append(caption);
    const notes = el("ul", null, { class: "footnotes" });
    if (record.histogram.some((row) => row.upper === null || row.upper === undefined)) {
      const open = record.histogram.find((row) => row.upper === null || row.upper === undefined);
      notes.append(el("li", `Hatched bar: the open bin (${count(open.lower)} bp and over) has no width; it is drawn from ${count(open.lower)} to ${count(open.lower + 200)} bp as a display convention. Its share is exact.`, { "data-footnote": "open-bin" }));
    }
    notes.append(el("li", "Shares rounded to one decimal; exact counts in the table.", { "data-footnote": "rounding" }));
    if (drawn.labelled === 0 && record.histogram.length <= chart.MANY_BINS) {
      notes.append(el("li", "Per-bar labels are in the table at this width."));
    }
    figure.append(notes);
    const tableWrap = el("div", null, { class: "table-wrap", role: "region", "aria-label": "Counts per bin", tabindex: "0", id: "histogram-table" });
    let showEvery = false;
    const drawTable = () => tableWrap.replaceChildren(chart.histogramTable(doc, data, showEvery));
    drawTable();
    figure.append(tableWrap);
    if (record.histogram.length > chart.MANY_BINS) {
      const toggle = el("button", "Show every bin", { type: "button", class: "control", "aria-controls": "histogram-table", "aria-pressed": "false" });
      toggle.addEventListener("click", () => {
        showEvery = !showEvery;
        toggle.textContent = showEvery ? "Show 10 bp rows" : "Show every bin";
        toggle.setAttribute("aria-pressed", showEvery ? "true" : "false");
        drawTable();
      });
      figure.append(toggle);
    }
    return figure;
  };

  const renderRecordBody = (record) => {
    const nodes = [];
    nodes.push(el("p", null, { class: "back" }));
    nodes[0].append(link("#/", "Back to records"));
    nodes.push(heading(record.label || record.short_id));
    const identity = el("p", `Reference ${record.reference_id}; policy ${policyText(record)}; record ${record.short_id}`, { class: "identity" });
    nodes.push(identity);
    if (record.label) nodes.push(el("p", "The title is an operator note, not part of the signed record.", { class: "help" }));
    const qualification = stateRow(record, "qualification");
    const trust = stateRow(record, "trust");
    const warnings = warningItems(record);
    const status = el("p", `${qualification.label}; ${trust.label}; ${warnings.length
      ? `${warnings.length} preflight warning${warnings.length === 1 ? "" : "s"}, listed below`
      : "no preflight warnings"}`, { class: "status-line", id: "record-status" });
    nodes.push(status);
    if (warnings.length) {
      const list = el("ul", null, { class: "warnings", id: "record-warnings", "aria-label": "Preflight warnings" });
      warnings.forEach((text) => list.append(el("li", text)));
      nodes.push(list);
    }
    nodes.push(chartFigure(record), denominator(record), whatItIs(record), howToRead(record), exactValues(record));
    const actions = el("p", null, { class: "toolbar" });
    const refresh = el("button", "Refresh", { type: "button", id: "refresh", class: "control" });
    refresh.addEventListener("click", () => render({ focus: false }));
    actions.append(refresh);
    nodes.push(actions);
    return nodes;
  };

  // On a route change the previous view is cleared at once, so nothing from
  // another record stays on screen while this one loads.
  const showLoading = (name, text) => {
    currentRecord = null;
    view().replaceChildren(heading(text));
    setState(name, "loading");
    live(text);
  };

  const renderRecord = async (recordId, focus) => {
    if (focus || !currentRecord || currentRecord.record_id !== recordId) showLoading("record", "Loading record…");
    else setState("record", "loading");
    const mine = generation;
    const { status, payload } = await getJson(`/api/v1/records/${recordId}`);
    if (mine !== generation) return;
    if (status === 401) { sessionEnded("record"); return; }
    const body = view();
    const back = el("p", null, { class: "back" });
    back.append(link("#/", "Back to records"));
    if (status === 404) {
      setState("record", "error");
      currentRecord = null;
      body.replaceChildren(back, heading("No record with this ID"), el("p", "It is not in this ROOT's catalog. Return to the records list."));
      live("No record with this ID");
    } else if (status === 503) {
      setState("record", "error");
      currentRecord = null;
      body.replaceChildren(back, heading("Record unavailable"), problemBox(
        "This record failed verification. Nothing is shown.",
        `Run ${verifyCommand(recordId)}`,
        null,
      ));
      live("This record failed verification");
    } else if (status !== 200 || !payload || payload.record_id !== recordId) {
      setState("record", "error");
      currentRecord = null;
      body.replaceChildren(back, heading("Record"), problemBox(`Could not load this record (HTTP ${status}).`, "Retry, or run traceback doctor.", () => render({ focus: false })));
      live("Could not load this record");
    } else {
      currentRecord = payload;
      body.replaceChildren(...renderRecordBody(payload));
      const state = !payload.eligible_alignments ? "empty" : (warningItems(payload).length ? "partial" : "success");
      setState("record", state);
      live(`Record ${recordName(payload)} loaded`);
    }
    if (focus) focusHeading();
  };

  // --- compare (hook for C6) ------------------------------------------------------
  const renderCompare = async (query, focus) => {
    const params = new URLSearchParams(query);
    const a = params.get("a");
    const b = params.get("b");
    const back = el("p", null, { class: "back" });
    back.append(link("#/", "Back to records"));
    if (!RECORD_ID.test(a || "") || !RECORD_ID.test(b || "") || a === b) {
      setState("compare", "empty");
      view().replaceChildren(back, heading("Compare two records"), el("p", "Select exactly 2 different records in the catalog, then choose Compare selected.", { class: "empty" }));
      live("Select exactly 2 records");
      if (focus) focusHeading();
      return;
    }
    showLoading("compare", "Loading both records…");
    if (!records) {
      const mine = generation;
      const { status, payload } = await getJson("/api/v1/records");
      if (mine !== generation) return;
      if (status === 401) { sessionEnded("compare"); return; }
      if (status === 200 && payload) records = payload;
    }
    const find = (id) => (records ? records.records.find((item) => item.record_id === id) : null);
    const list = el("ul", null, { class: "compare-list" });
    let missing = 0;
    [["A (earlier import)", a], ["B", b]].forEach(([role, id]) => {
      const item = find(id);
      const li = el("li", `${role}: `);
      if (item && item.status === "verified") {
        li.append(link(`#/records/${id}`, recordName(item)));
      } else {
        missing += 1;
        li.append(el("span", item ? `${recordName(item)} (${item.status_label})` : "not in this catalog"));
      }
      list.append(li);
    });
    view().replaceChildren(
      back,
      heading("Compare two records"),
      el("p", "Side-by-side comparison is not built yet. Open each record on its own:", { class: "help" }),
      list,
    );
    setState("compare", missing ? "partial" : "success");
    live("Comparison is not built yet");
    if (focus) focusHeading();
  };

  // --- router -------------------------------------------------------------------
  const render = async ({ focus }) => {
    generation += 1;
    const hash = window.location.hash || "#/";
    const record = RECORD_ROUTE.exec(hash);
    const compare = COMPARE_ROUTE.exec(hash);
    if (record) await renderRecord(record[1], focus);
    else if (compare) await renderCompare(compare[1], focus);
    else await renderCatalog(focus);
  };
  const onRoute = () => {
    if (!started) return;
    const hash = window.location.hash || "#/";
    const changed = hash !== lastRoute;
    lastRoute = hash;
    render({ focus: changed });
  };

  const start = () => {
    if (started) return;
    started = true;
    site().hidden = false;
    lastRoute = window.location.hash || "#/";
    render({ focus: false });
    window.addEventListener("hashchange", onRoute);
    window.addEventListener("focus", () => {
      if (!started || inflight) return;
      loadJobs();
      render({ focus: false });
    });
    if (compactQuery && typeof compactQuery.addEventListener === "function") {
      compactQuery.addEventListener("change", () => {
        const match = RECORD_ROUTE.exec(window.location.hash || "");
        if (currentRecord && match && currentRecord.record_id === match[1]) {
          view().replaceChildren(...renderRecordBody(currentRecord));
        }
      });
    }
  };

  window.addEventListener("traceback:jobs", (event) => {
    jobs = event.detail && event.detail.jobs ? event.detail : "unavailable";
    renderJobs();
  });
  window.addEventListener("traceback:operator", start);
  window.addEventListener("traceback:session-ended", () => clearSelection());
})();
