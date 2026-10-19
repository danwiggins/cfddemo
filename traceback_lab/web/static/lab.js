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
  if (!window.TracebackLabFormat?.formatMetric || !window.TracebackLabFormat?.formatResultMetric) {
    const message = "Lab display unavailable: metric formatter did not load. Reload this page.";
    const main = document.getElementById("main");
    if (main) main.textContent = message;
    const status = document.getElementById("status");
    if (status) status.textContent = message;
    document.body.dataset.ready = "0";
    return;
  }

  const POLL_MS = 15000;
  const MISSED_LIMIT = 3;
  const LENSES = [
    // Fixed order; a lens that is not built yet is absent (S6c adds the rest).
    { id: "trust-qc", label: "Trust/QC" },
  ];
  const state = {
    samples: [],
    rootHasRuns: false,
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
    sessionExpired: false,
    connectionLost: false,
    disconnectedSince: null,
    resultStale: false,
    resultConfirmedAt: null,
    selectedRefresh: null,
    loadFailure: null,
    initialLoadFailure: false,
    refreshFailureKind: null,
    pollMessage: "",
    pollAction: null,
  };
  window.TracebackLab = { state, LENSES, h, render, chart, dataTable, rawCell, fmt, fmtMetric, fmtMetricValue, fmtCount,
    requestJson, refreshSelectedSample,
    formatMetric: window.TracebackLabFormat.formatMetric,
    formatResultMetric: window.TracebackLabFormat.formatResultMetric };

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
    if (tag === "div" && el.classList.contains("table-wrap")) {
      if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "0");
      if (!el.hasAttribute("role")) el.setAttribute("role", "region");
      if (!el.hasAttribute("aria-label")) {
        const caption = el.querySelector("table caption")?.textContent?.trim();
        el.setAttribute("aria-label", caption || "Scrollable data table");
      }
    }
    if (tag === "table") {
      const caption = el.querySelector(":scope > caption")?.textContent?.trim() || "Table";
      const headers = [...el.querySelectorAll(":scope > thead > tr > th")].map((th) => th.textContent.trim());
      [...el.querySelectorAll(":scope > tbody > tr")].forEach((row, rowIndex) => {
        const subject = row.querySelector(":scope > th[scope='row']")?.textContent?.trim() || `row ${rowIndex + 1}`;
        [...row.children].forEach((cell, columnIndex) => {
          cell.querySelectorAll("details.raw-value > summary").forEach((summary) => {
            if (!summary.hasAttribute("aria-label")) summary.setAttribute("aria-label",
              `Raw value: ${caption}, ${subject}, ${headers[columnIndex] || "value"}`);
          });
        });
      });
    }
    // Async route content may arrive after the disconnected state was set.
    if ((tag === "button" || tag === "select") && "data-requires-server" in (attrs || {})) {
      if (document.body.dataset.disconnected === "1" &&
          !(attrs.id === "readiness-refresh" && !state.connectionLost && !state.sessionExpired)) {
        setFetchControlEnabled(el, false);
      }
    }
    if (tag === "a" && typeof attrs?.href === "string" && attrs.href.startsWith("#/") &&
        document.body.dataset.disconnected === "1" && !isLocalRecordedRoute(attrs.href)) setLinkEnabled(el, false);
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

  function fmt(value, unit, options = {}) {
    return window.TracebackLabFormat.formatMetric(value, unit, {
      missingUnit: unit !== undefined, ...options,
    }).text;
  }
  function fmtMetric(metric, options = {}) {
    return window.TracebackLabFormat.formatResultMetric(metric, options).text;
  }
  function fmtMetricValue(metric, value, options = {}) {
    return fmtMetric({ ...metric, value }, { reason: null, ...options });
  }
  function fmtCount(value) {
    return window.TracebackLabFormat.formatMetric(value, null, {
      valueKind: "count", missingUnit: false,
    }).text;
  }

  // -- session ------------------------------------------------------------------------
  async function exchangeBootstrap() {
    const params = new URLSearchParams(location.hash.slice(1));
    const code = params.get("bootstrap");
    if (!code) return;
    // An optional start route travels with the one-use link (e.g. for a deep link).
    const route = params.get("route");
    const safe = route && /^\/[A-Za-z0-9/?=&._-]{0,383}$/.test(route) ? route : "/";
    history.replaceState(null, "", location.pathname + "#" + safe);
    const response = await fetch("/api/v1/session/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bootstrap: code }),
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("bootstrap");
  }

  function requestIssue(kind) {
    return Object.assign(new Error(kind), { kind, session: kind === "session" });
  }

  async function requestJson(path, { signal, timeoutMs = 10000, isCurrent = () => true,
    allow404 = false } = {}) {
    const controller = new AbortController();
    let timeoutId;
    let timedOut = false;
    let settled = false;
    const abort = () => controller.abort();
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    }
    const deadline = timeoutMs === null ? null : new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        timedOut = true;
        settled = true;
        controller.abort();
        reject(requestIssue("timeout"));
      }, timeoutMs);
    });
    try {
      const read = (async () => {
        const response = await fetch(path, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (settled || !isCurrent()) throw requestIssue("obsolete");
        if (response.status === 401) {
          expireSession();
          throw requestIssue("session");
        }
        if (!response.ok && !(allow404 && response.status === 404)) throw requestIssue("unavailable");
        let payload;
        try { payload = await response.json(); }
        catch (_error) { throw requestIssue("invalid"); }
        if (settled || !isCurrent()) throw requestIssue("obsolete");
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw requestIssue("invalid");
        return payload;
      })();
      return await (deadline ? Promise.race([read, deadline]) : read);
    } catch (error) {
      if (!isCurrent()) throw requestIssue("obsolete");
      if (error.kind) throw error;
      throw requestIssue(timedOut ? "timeout" : "unavailable");
    } finally {
      settled = true;
      clearTimeout(timeoutId);
      signal?.removeEventListener("abort", abort);
    }
  }

  function api(path, signal, options = {}) {
    return requestJson(path, { signal, allow404: /^\/api\/samples\/[0-9a-f]{12,64}$/.test(path), ...options });
  }

  function expireSession() {
    if (state.sessionExpired) return;
    state.sessionExpired = true;
    state.resultStale = !!state.sample;
    state.navigationRevision += 1;
    window.TracebackLabReportUI?.invalidate?.();
    restoreRenderedRoute();
    document.body.classList.add("dimmed");
    setControlsEnabled(false);
    setStatus("Session expired. A new local session is needed to load or export results. Restart `traceback lab serve --open` for a fresh one-use link.");
    renderRecoveryState();
  }

  // -- routes -------------------------------------------------------------------------
  function parseContext(params, query, routeLength) {
    const keys = ["algorithm", "metric", "family"];
    const context = { algorithm: null, metric: null, family: null };
    const known = new URLSearchParams();
    let invalid = false;
    for (const key of keys) {
      const values = params.getAll(key);
      if (values.length > 1) invalid = true;
      for (const value of values) known.append(key, value);
      if (values.length === 1) {
        if (/^[A-Za-z0-9_-]{1,64}$/.test(values[0])) context[key] = values[0];
        else invalid = true;
      }
    }
    if (query.length > 256 || routeLength > 384 || known.toString().length > 256 ||
        (context.metric && !context.algorithm)) invalid = true;
    return invalid
      ? { context: { algorithm: null, metric: null, family: null }, contextInvalid: true,
        contextError: "Context link is invalid; showing the selected sample." }
      : { context, contextInvalid: false, contextError: null };
  }

  function parseRoute() {
    const hash = location.hash.replace(/^#/, "") || "/";
    const [pathPart, query = ""] = hash.split("?");
    const params = new URLSearchParams(query);
    let match = pathPart.match(/^\/sample\/([0-9a-f]{12,64})\/([a-z-]+)$/);
    if (match) return { name: "lens", id12: match[1], lens: match[2], ...parseContext(params, query, hash.length) };
    if (pathPart === "/" || pathPart === "") {
      const sample = params.get("sample");
      if (sample && !/^[0-9a-f]{12,64}$/.test(sample)) return { name: "unknown", id12: sample };
      return { name: "home", id12: sample };
    }
    if (pathPart === "/about/clia") return { name: "clia" };
    return { name: "unknown", id12: null };
  }

  function sampleIdentityMatches(payload, requested) {
    return typeof payload.id12 === "string" && /^[0-9a-f]{12,64}$/.test(payload.id12) &&
      typeof payload.source_id === "string" && /^[0-9a-f]{64}$/.test(payload.source_id) &&
      payload.source_id.startsWith(payload.id12) && payload.source_id.startsWith(requested);
  }

  async function loadSample(id12, isCurrent = () => true) {
    if (!id12) return null;
    if (state.sample && state.sample.id12 === id12) return state.sample;
    const payload = await api(`/api/samples/${id12}`, null, { isCurrent });
    if (typeof payload.found !== "boolean") throw requestIssue("invalid");
    if (payload.found && !sampleIdentityMatches(payload, id12)) throw requestIssue("invalid");
    if (payload.found && !["completed_input_unavailable", "completion_history_unavailable"].includes(payload.state) &&
        (!Array.isArray(payload.algorithms) || !Array.isArray(payload.catalog))) throw requestIssue("invalid");
    return payload.found ? payload : { found: false, id12, candidates: payload.candidates || [] };
  }

  async function navigate() {
    if (state.sessionExpired) { restoreRenderedRoute(); return; }
    const revision = ++state.navigationRevision;
    const requestedHash = location.hash || "#/";
    const route = parseRoute();
    const target = route.id12 || (route.name === "home" ? state.defaultSampleId12 : null);
    window.TracebackLabReportUI?.selectionChanged(target);
    const isCurrent = () => revision === state.navigationRevision && (location.hash || "#/") === requestedHash;
    try {
      const sample = route.name === "home" || route.name === "lens" ? await loadSample(target, isCurrent) : state.sample;
      if (!isCurrent()) return;
      const freshSample = sample && sample !== state.sample;
      if (freshSample) state.runLogLines.clear();
      if (freshSample) {
        state.resultConfirmedAt = new Date();
        state.resultStale = false;
      }
      state.sample = sample;
      state.route = route;
      state.loadFailure = null;
      render();
      state.renderedHash = requestedHash;
      if (!state.resultStale && !state.connectionLost) {
        setControlsEnabled(true);
        document.body.classList.remove("dimmed");
      }
      const title = document.querySelector("main h1");
      if (title) { title.setAttribute("tabindex", "-1"); title.focus({ preventScroll: false }); }
      revealCurrentNav();
    } catch (error) {
      if (!isCurrent() || state.sessionExpired) return;
      state.loadFailure = { requestedHash, id12: target, kind: error.kind || "unavailable" };
      state.resultStale = !!state.sample;
      if (state.renderedHash) {
        history.replaceState(null, "", location.pathname + location.search + state.renderedHash);
        renderSwitcher();
      } else {
        state.route = { name: "load-error" };
        state.sample = null;
        state.loading = false;
        render();
      }
      renderRecoveryState();
    }
  }

  function refreshSelectedSample() {
    const previous = state.sample;
    if (state.sessionExpired) return Promise.resolve({ kind: "session" });
    if (!previous?.found) return Promise.resolve({ kind: "unavailable" });
    const hash = location.hash || "#/";
    const key = `${previous.id12}:${previous.full_input_id || "unknown"}:${hash}`;
    if (state.selectedRefresh?.key === key &&
        state.selectedRefresh.revision === state.navigationRevision) return state.selectedRefresh.promise;
    const revision = ++state.navigationRevision;
    const isCurrent = () => revision === state.navigationRevision && hash === (location.hash || "#/") &&
      previous.id12 === state.sample?.id12 && previous.full_input_id === state.sample?.full_input_id;
    const promise = (async () => {
      try {
        const current = await requestJson(`/api/samples/${encodeURIComponent(previous.id12)}`, { isCurrent });
        if (!isCurrent()) return { kind: "obsolete" };
        if (current.found !== true || !sampleIdentityMatches(current, previous.id12) ||
            current.id12 !== previous.id12 || current.source_id !== previous.source_id ||
            (!["completed_input_unavailable", "completion_history_unavailable"].includes(current.state) &&
              (!Array.isArray(current.catalog) || !Array.isArray(current.algorithms)))) {
          throw requestIssue("invalid");
        }
        const checkedAt = new Date();
        state.sample = current;
        state.resultConfirmedAt = checkedAt;
        state.resultStale = false;
        state.connectionLost = false;
        state.refreshFailureKind = null;
        state.readinessRefresh = { ...state.readinessRefresh,
          [`${current.id12}:${current.full_input_id || "unknown"}`]: { checkedAt: checkedAt.toISOString(), stale: false } };
        const focused = document.activeElement;
        const focusedId = focused?.id;
        const focusedHref = focused instanceof HTMLAnchorElement ? focused.getAttribute("href") : null;
        const focusedHeading = focused?.matches("#main h1");
        if (state.route.name === "lens" && state.route.lens === "methods") {
          state.methodContextApplied = `${revision}:${hash}`;
        }
        render();
        const replacement = focusedHref
          ? [...document.querySelectorAll("a[href]")].find((link) => link.getAttribute("href") === focusedHref)
          : focusedId ? document.getElementById(focusedId)
            : focusedHeading ? document.querySelector("#main h1") : null;
        if (replacement) {
          if (focusedHeading) replacement.tabIndex = -1;
          replacement.focus({ preventScroll: true });
        }
        setControlsEnabled(true);
        renderRecoveryState();
        return { kind: "ok", sample: current };
      } catch (error) {
        if (state.sessionExpired) return { kind: "session" };
        if (!isCurrent()) return { kind: "obsolete" };
        state.resultStale = true;
        state.refreshFailureKind = error.kind || "unavailable";
        setControlsEnabled(false);
        state.readinessRefresh = { ...state.readinessRefresh,
          [`${previous.id12}:${previous.full_input_id || "unknown"}`]: {
            ...state.readinessRefresh?.[`${previous.id12}:${previous.full_input_id || "unknown"}`], stale: true } };
        renderRecoveryState();
        return { kind: error.kind || "unavailable" };
      } finally {
        if (state.selectedRefresh?.key === key && state.selectedRefresh.revision === revision) {
          state.selectedRefresh = null;
          const refreshButton = document.getElementById("readiness-refresh");
          if (refreshButton && !state.sessionExpired && !state.connectionLost) {
            refreshButton.disabled = false;
          }
          renderRecoveryState();
        }
      }
    })();
    state.selectedRefresh = { key, revision, promise };
    renderRecoveryState();
    return promise;
  }

  // -- render -------------------------------------------------------------------------
  function setStatus(text, action) {
    const status = document.getElementById("status");
    let message = status.querySelector("[data-status-message]");
    if (!message) {
      message = h("span", { "data-status-message": "" });
      status.prepend(message);
    }
    if (message.textContent !== (text || "")) message.textContent = text || "";
    status.querySelector("[data-library-error]")?.remove();
    let reload = status.querySelector("button.reload");
    if (action) {
      if (!reload) {
        reload = h("button", { type: "button", class: "control reload", "data-requires-server": "", onclick: () => location.reload() }, action);
        status.append(h("span", { "data-reload-action": "" }, " ", reload));
      } else reload.textContent = action;
    } else if (reload) reload.closest("[data-reload-action]").remove();
  }

  function setPollStatus(text, action = null) {
    state.pollMessage = text;
    state.pollAction = action;
    setStatus(text, action);
  }

  function recoveryCause(kind) {
    return kind === "timeout" ? "The request timed out." :
      kind === "invalid" ? "The response could not be read." :
      "The Lab server could not provide this result.";
  }

  function renderRecoveryState() {
    const freshness = document.getElementById("freshness-state");
    if (!freshness) return;
    const run = document.getElementById("run-state");
    const result = document.getElementById("result-state");
    const action = document.getElementById("recovery-action");
    const last = state.resultConfirmedAt ? state.resultConfirmedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "unknown";
    freshness.dataset.stale = state.resultStale || state.sessionExpired ? "true" : "false";
    freshness.textContent = state.sessionExpired ? `Freshness/session: session expired; last confirmed ${last}.` :
      state.connectionLost ? `Freshness/session: connection lost; showing last confirmed result (${last}).` :
      state.resultStale ? `Freshness/session: ${state.selectedRefresh ? "verifying selected result" : "selected result unconfirmed"}; last confirmed ${last}.` :
      state.sample?.found ? `Freshness/session: recorded projection confirmed at ${last}.` :
      "Freshness/session: no selected result loaded.";
    const status = state.lastStatus;
    run.textContent = status?.active ? (state.connectionLost || state.sessionExpired
      ? "Run state: a run was in progress at last check; current status is unknown."
      : "Run state: a run is in progress; results below are from completed steps.") :
      status?.interrupted ? "Run state: the last run was interrupted; published results may still be available." :
      status?.run_log_unavailable ? "Run state: the last run log is unavailable." :
      state.connectionLost || state.sessionExpired ? "Run state: current status is unknown." :
      "Run state: no active run was reported at last check.";
    const generationChanged = Number.isFinite(status?.generation) &&
      Number.isFinite(state.generationAtLoad) && status.generation > state.generationAtLoad;
    if (generationChanged) run.append(" Results changed on disk; full Reload is still needed for the sample listing.");
    const sample = state.sample;
    const algorithms = Array.isArray(sample?.algorithms) ? sample.algorithms : [];
    const failed = algorithms.filter((row) => row.status === "failed").length;
    const skipped = algorithms.filter((row) => row.status === "skipped").length;
    const missing = sample?.state === "completed_input_unavailable" ||
      (!sample && state.completedInputUnavailable);
    const historyMissing = sample?.state === "completion_history_unavailable" ||
      (!sample && state.completionHistoryUnavailable);
    if (missing) result.textContent = "Recorded result: selected input unavailable; no result is asserted for it.";
    else if (historyMissing) result.textContent = "Recorded result: completion history unavailable; the latest completion cannot be verified.";
    else if (sample?.found) {
      result.textContent = !algorithms.length ? "Recorded result: no completed method result for this selected input." : failed || skipped
        ? `Recorded result: partially available (${failed} failed, ${skipped} skipped methods); reasons appear beside affected methods.`
        : "Recorded result: available for the selected input; inspect each method's reliability before interpretation.";
    } else result.textContent = "Recorded result: no selected result available.";
    action.replaceChildren();
    let problem = null;
    if (state.sessionExpired) {
      problem = "Session expired. A new local session is needed to load or export results.";
      action.append("Next action: restart `traceback lab serve --open` for a fresh one-use link.");
    } else if (missing || historyMissing) {
      problem = missing ? "Selected input unavailable. Restore the input or choose another recorded sample." :
        "Completion history unavailable. The latest completion cannot be verified.";
      if (missing) {
        const other = state.samples.find((row) => row.id12 !== sample?.id12 &&
          row.state !== "completed_input_unavailable" && row.state !== "completion_history_unavailable");
        if (other) action.append("Next action: ", h("a", { href: `#/?sample=${other.id12}` }, "Choose an available sample"));
      }
      if (!action.hasChildNodes()) action.append("Next action: ", h("a", {
        href: "https://github.com/danwiggins/cfddemo/blob/main/docs/LAB-GUIDE.md#troubleshooting",
        target: "_blank", rel: "noopener noreferrer", referrerpolicy: "no-referrer",
      }, "Open recovery guide (external)"));
    } else if (state.loadFailure) {
      problem = `Could not open selected sample. ${recoveryCause(state.loadFailure.kind)} The last rendered view is retained and marked stale.`;
      const requested = state.loadFailure.requestedHash;
      action.append("Next action: ", h("button", { type: "button", class: "control",
        onclick: () => {
          history.replaceState(null, "", location.pathname + location.search + requested);
          navigate();
        } }, "Retry loading this selection"));
    } else if (state.initialLoadFailure) {
      problem = "Lab results could not be loaded. The sample list and current result are unknown.";
      action.append("Next action: ", h("button", { type: "button", class: "control",
        onclick: () => location.reload() }, "Retry loading Lab"));
    } else if (state.resultStale || state.connectionLost) {
      problem = state.connectionLost ? `Lab server not responding since ${state.disconnectedSince || "page load"}; showing last confirmed result.` :
        `Selected result could not be refreshed. ${recoveryCause(state.refreshFailureKind)} Showing last confirmed result.`;
      const retry = h("button", { type: "button", class: "control", onclick: () => {
        if (state.connectionLost) poll(); else if (generationChanged) location.reload(); else refreshSelectedSample();
      } }, state.connectionLost ? "Retry connection" : generationChanged ? "Reload Lab" : "Retry selected refresh");
      retry.disabled = !!state.selectedRefresh;
      action.append("Next action: ", retry);
    } else if (!state.loading && !state.samples.length) {
      action.append("Next action: ", h("a", {
        href: "https://github.com/danwiggins/cfddemo/blob/main/docs/LAB-GUIDE.md#first-five-minutes",
        target: "_blank", rel: "noopener noreferrer", referrerpolicy: "no-referrer",
      }, "Open example setup guide (external)"));
    } else if (sample?.found && (failed || skipped) && !state.pollAction) {
      action.append("Next action: ", h("a", { href: `#/sample/${sample.id12}/methods` }, "Method readiness"));
    } else if (status?.active && !state.pollAction) {
      action.append("Next action: ", h("a", { href: sample?.id12 ? `#/sample/${sample.id12}/trust-qc` : "#/" }, "View run status"));
    }
    if (problem) setStatus(problem);
    else if (!state.loading) setStatus(state.pollMessage, state.pollAction);
  }

  function renderSwitcher() {
    const select = document.getElementById("sample-switcher");
    select.replaceChildren(
      ...(state.sample && state.sample.found ? [] : [h("option", { value: "", selected: true, disabled: true }, "Select a sample")]),
      ...state.samples.map((row) =>
        h("option", { value: row.id12, selected: state.sample && state.sample.id12 === row.id12 },
          `${row.id12} · ${row.run_utc ? row.run_utc.slice(0, 10) : "—"} · ${fmt(row.depth_x, "x")}${row.example ? " · EXAMPLE (synthetic)" : ""}${row.state === "completed_input_unavailable" ? " · completed input unavailable" : row.state === "completion_history_unavailable" ? " · completion history unavailable" : ""}`)
      )
    );
  }

  function renderSampleContext() {
    const context = document.getElementById("sample-context");
    const sample = state.sample && state.sample.found && state.route.name !== "unknown" ? state.sample : null;
    if (!sample) {
      const requested = state.route.id12 || state.sample?.id12;
      const safeId = typeof requested === "string" && /^[0-9a-f]{12,64}$/.test(requested) ? requested : null;
      context.replaceChildren(state.loading ? "Loading sample…" :
        state.initialLoadFailure ? "Sample list could not be loaded" :
          state.route.name === "load-error" ? `Requested sample ${safeId || "identity"} could not be loaded` :
            state.route.name === "unknown" || state.sample?.found === false ?
              `Unknown requested sample${safeId ? ` ${safeId}` : ""}` : "No sample selected");
      return;
    }
    const listed = state.samples.find((row) => row.id12 === sample.id12);
    const example = sample.example ?? listed?.example;
    const kind = example === undefined ? "Synthetic status unavailable"
      : example ? "EXAMPLE (synthetic)" : "Not marked as the built-in synthetic example";
    const availability = sample.state === "completed_input_unavailable" ? "completed input unavailable"
      : sample.state === "completion_history_unavailable" ? "completion history unavailable"
        : sample.completed_utc ? `completion recorded ${sample.completed_utc.slice(0, 10)}`
          : sample.completed_utc === null ? "no completion recorded for this sample"
            : "selected sample";
    context.replaceChildren(`Sample ${sample.id12} · `,
      h("span", { class: "sample-context-kind" }, kind), ` · ${availability}`);
  }

  function updateNavFade() {
    const nav = document.getElementById("lenses");
    const wrapper = nav.closest(".lens-navigation");
    if (wrapper) wrapper.hidden = nav.hidden;
    // Measure against the full slot so a resize can remove controls once all
    // labels fit without them; measuring the narrowed scroller would keep them.
    const overflow = !nav.hidden && nav.scrollWidth > (wrapper?.clientWidth ?? nav.clientWidth) + 1;
    nav.classList.toggle("more-left", overflow && nav.scrollLeft > 1);
    nav.classList.toggle("more-right", overflow && nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 1);
    if (wrapper) {
      wrapper.classList.toggle("has-overflow", overflow);
      wrapper.querySelector(".lens-scroll-prev").disabled = !overflow || nav.scrollLeft <= 1;
      wrapper.querySelector(".lens-scroll-next").disabled = !overflow ||
        nav.scrollLeft + nav.clientWidth >= nav.scrollWidth - 1;
    }
  }

  function setupLensNavigation() {
    const nav = document.getElementById("lenses");
    const wrapper = h("div", { class: "lens-navigation" });
    const previous = h("button", { type: "button", class: "lens-scroll-control lens-scroll-prev",
      "aria-label": "Previous views", "aria-controls": "lenses",
      onclick: () => { nav.scrollLeft -= nav.clientWidth; updateNavFade(); } }, "‹");
    const next = h("button", { type: "button", class: "lens-scroll-control lens-scroll-next",
      "aria-label": "More views", "aria-controls": "lenses",
      onclick: () => { nav.scrollLeft += nav.clientWidth; updateNavFade(); } }, "›");
    nav.before(wrapper);
    wrapper.append(previous, nav, next);
    nav.addEventListener("focusin", (event) => {
      // Pointer focus can happen between mouse down and up. Moving the link
      // then sends the click to the nav instead of activating its route.
      if (!(event.target instanceof HTMLAnchorElement) || !event.target.matches(":focus-visible") ||
          getComputedStyle(nav).flexWrap !== "nowrap") return;
      const frame = nav.getBoundingClientRect();
      const link = event.target.getBoundingClientRect();
      // Keep focus clear of the 1.25rem edge fade as keyboard travel scrolls.
      const fadeWidth = parseFloat(getComputedStyle(document.documentElement).fontSize) * 1.25;
      if (link.left < frame.left + fadeWidth || link.right > frame.right - fadeWidth) {
        nav.scrollLeft += link.left - frame.left - (nav.clientWidth - link.width) / 2;
      }
      updateNavFade();
    });
  }

  function revealCurrentNav() {
    const nav = document.getElementById("lenses");
    // The previous loading/home render hid the wrapper. Give it its final
    // visible width and control slots before measuring the active link.
    updateNavFade();
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
    updateNavFade();
  }

  function render() {
    renderSwitcher();
    renderSampleContext();
    renderNav();
    const main = document.getElementById("main");
    main.replaceChildren();
    renderRecoveryState();
    const route = state.route;
    if (state.loading) {
      setStatus("Loading Lab results…");
      main.append(h("h1", { class: route.name === "home" ? "visually-hidden" : "view-title" }, "Loading…"));
      if (route.name === "home") {
        main.append(
          h("section", { class: "panel landing-intro loading-intro" },
            h("h2", {}, "Sample overview"), h("p", {}, "Loading…")),
          h("p", { class: "headline loading-headline" }, "Loading…"),
          h("div", { class: "landing-band" },
            h("section", { class: "panel defended loading-defended" }, h("h2", {}, "Defended results"), h("p", {}, "Loading…")),
            h("section", { class: "panel curve loading-curve" }, h("h2", {}, "Depth → utility"), h("p", {}, "Loading…")),
            h("section", { class: "panel controls loading-controls" }, h("h2", {}, "Positive controls"), h("p", {}, "Loading…"))),
          h("section", { class: "clia-panel loading-clia" },
            h("h2", { class: "clia-title" }, "What CLIA means for this demo"), h("p", {}, "Loading…")),
        );
      } else main.append(h("section", { class: "panel loading-lens" }, h("p", {}, "Loading…")));
      return;
    }
    if (route.name === "load-error") {
      main.append(h("h1", { class: "view-title" }, "Could not open selected sample"),
        h("p", { class: "problem" }, "The Lab result could not be read. Retry loading this selection when the server is available."));
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
        const anchor = route.name === "home" ? main.querySelector(".landing-intro") : main.querySelector("h1");
        if (anchor) anchor.after(notice); else main.prepend(notice);
      }
      window.TracebackLabReportUI?.mount(main, sample);
    };
    if (route.name === "home" && views.landing) { appendSample(views.landing(sample)); return; }
    const chosen = lens || LENSES[0].id;
    if (chosen === "trust-qc") { appendSample(trustQc(sample)); return; }
    if (views[chosen]) { appendSample(views[chosen](sample)); return; }
    main.append(h("h1", { class: "view-title" }, "Unknown sample"), h("p", {}, "No such lens. ", h("a", { href: "#/" }, "Back to Lab home")));
  }

  function emptyState() {
    const copy = (text) => h("div", { class: "onboarding-command" }, h("code", {}, text),
      h("button", { type: "button", class: "control", "aria-label": `Copy command: ${text}`,
        onclick: () => navigator.clipboard && navigator.clipboard.writeText(text) }, "Copy command"));
    return [
      h("h1", { class: "view-title" }, "Traceback Lab"),
      h("p", { class: "empty" }, state.rootHasRuns
        ? "No Lab samples are available under this server's ROOT, although run history exists. Inspect the run log with `uv run traceback lab runs --root ROOT_PATH`. Replace ROOT_PATH below with the ROOT shown where `lab serve` started."
        : "No Lab runs under this server's ROOT. Use the same ROOT shown in the terminal where `lab serve` started. Replace ROOT_PATH in the commands below with that value."),
      h("section", { class: "panel onboarding-guide", "aria-labelledby": "onboarding-example-title" },
        h("h2", { id: "onboarding-example-title" }, "Start with a synthetic example"),
        h("p", {}, "From the repository checkout, run these commands in a terminal. The page does not run them for you."),
        copy("uv sync"),
        h("ol", { class: "onboarding-steps" },
          h("li", {}, h("strong", {}, "Check setup"), copy("uv run traceback lab doctor --root ROOT_PATH")),
          h("li", {}, h("strong", {}, "Generate the example"), copy("uv run traceback lab run --example --root ROOT_PATH")),
          h("li", {}, h("strong", {}, "Open the local page"),
            h("p", {}, "This page is already being served. If you stopped the server, restart it with the same ROOT:"),
            copy("uv run traceback lab serve --open --root ROOT_PATH")),
          h("li", {}, h("strong", {}, "Explore signals"),
            h("p", {}, "Reload this page after the example finishes, then choose Explore signals on its landing. Check reliability before interpreting a result."),
            h("button", { type: "button", class: "control", "data-requires-server": "", onclick: () => location.reload() }, "Reload Lab"))),
        h("p", {}, h("a", { href: "https://github.com/danwiggins/cfddemo/blob/main/docs/LAB-GUIDE.md#first-five-minutes", target: "_blank", rel: "noreferrer noopener" }, "Full example guide on GitHub (external)")),
        h("p", { class: "help" }, "The example is synthetic. Completing setup does not establish scientific or clinical qualification.")),
      h("section", { class: "panel onboarding-real", "aria-labelledby": "onboarding-real-title" },
        h("h2", { id: "onboarding-real-title" }, "Prepare a real input"),
        h("p", {}, "Use a real-kit reference and the protocol required by each method. The built-in example reference is not suitable for real input."),
        h("ol", { class: "onboarding-steps" },
          h("li", {}, "Check samtools ≥ 1.20 and minimap2 ≥ 2.28, then run doctor with the same ROOT."),
          h("li", {}, "Register a verified hg38 reference for real input.", copy("uv run traceback reference register --fasta HG38_FASTA --id hg38 --root ROOT_PATH")),
          h("li", {}, "Confirm platform, basecall/modification channels, depth, and any method-specific assets before a run."),
          h("li", {}, "Select your own chunk directory and run with the registered reference.", copy("uv run traceback lab run CHUNK_DIR --reference hg38 --root ROOT_PATH"))),
        h("p", {}, h("a", { href: "https://github.com/danwiggins/cfddemo/blob/main/docs/LAB-GUIDE.md#one-time-setup-for-real-kits", target: "_blank", rel: "noreferrer noopener" }, "Real-kit setup guide on GitHub (external)")),
        h("p", { class: "help" }, "Doctor checks the environment. A successful setup does not make a result available or validate a scientific interpretation.")),
      window.TracebackLabViews.landingCliaPanel(),
    ];
  }

  // -- charts ---------------------------------------------------------------------------
  function rawCell(display, raw, unit, context = null) {
    const exact = raw === null || raw === undefined ? "null" : String(raw);
    return h("div", { class: "value-with-raw" }, h("span", { class: "display-value" }, display), " ",
      h("details", { class: "raw-value" }, h("summary", { "aria-label": context ? `Raw value: ${context}` : null }, "Raw value"),
        h("code", {}, `${exact} ${unit || "unit not declared"}`)));
  }
  function chartCell(value) {
    if (value instanceof Node) return value;
    return typeof value === "number" && Number.isFinite(value) ? String(value) : fmt(value);
  }
  function dataTable(caption, headers, rows) {
    const table = h("table", { class: "data-table" },
      h("caption", {}, caption),
      h("thead", {}, h("tr", {}, ...headers.map((name) => h("th", { scope: "col" }, name)))),
      h("tbody", {}, ...rows.map((row) => h("tr", {}, ...row.map((cell, index) => {
        if (cell instanceof Node && cell.classList?.contains("value-with-raw")) {
          const subject = row[0] instanceof Node ? row[0].textContent : String(row[0]);
          cell.querySelector("details.raw-value summary")?.setAttribute("aria-label",
            `Raw value: ${caption}, ${subject}, ${headers[index]}`);
        }
        return h("td", {}, chartCell(cell));
      })))));
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
      } catch (_error) {
        box.replaceWith(h("div", { class: "problem" }, "Chart could not be drawn. The data table below has the same numbers."));
        const details = wrap.querySelector("details");
        if (details) details.open = true;
      }
    });
    return wrap;
  }

  async function fetchArtifact(sample, label, algorithmId, panelId) {
    const revision = state.navigationRevision;
    const hash = location.hash;
    return requestJson(`/api/samples/${sample.id12}/artifact/${label}/${algorithmId}/${panelId}`, {
      isCurrent: () => revision === state.navigationRevision && hash === location.hash &&
        state.sample?.id12 === sample.id12,
    });
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
      if (error.kind === "obsolete" || !slot.isConnected) return;
      // No panel data reached the page, so there is no data table to open;
      // the summary metrics stay in the table below.
      const rows = algorithm.metrics.map((m) => [m.label, fmtMetric(m, { algorithmId: algorithm.algorithm_id })]);
      slot.replaceWith(h("div", {},
        h("div", { class: "problem" }, "Chart could not be drawn. Retry loading this selected result; summary numbers are below."),
        h("div", { class: "table-wrap" }, h("table", { class: "data-table" }, h("caption", {}, "fragment-size summary"),
          h("tbody", {}, ...rows.map(([label, value]) => h("tr", {}, h("th", { scope: "row" }, label), h("td", {}, value))))))));
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
        ? `needs ${fmt(a.min_depth_x, "x")}; this run ${fmt(depth, "x")} → ${a.status === "skipped" ? `skipped (${a.skip_reason})` : a.status === "failed" ? "failed" : "ran"}`
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
      const duration = fmt(a.duration_s, "s");
      const durationText = runNote === "latest run in progress" && Number.isFinite(a.duration_s)
        ? `${duration} (selected result)` : duration;
      const durationNode = Number.isFinite(a.duration_s)
        ? rawCell(durationText, a.duration_s, "s", `${a.algorithm_id}, selected result duration`)
        : h("span", { class: "display-value" }, durationText);
      durationNode.querySelector(".display-value")?.setAttribute("data-run-duration-display", "");
      const cells = [h("th", { scope: "row" }, a.algorithm_id + (a.hidden ? " (intermediate)" : "")),
        h("td", { class: a.status === "failed" ? "problem" : null,
          "data-run-algorithm": a.algorithm_id, "data-run-input": a.provenance?.input_id || "" }, status, " ",
          h("span", { class: "state-chip run-note", "data-run-note": "", hidden: !runNote }, runNote || "")),
        h("td", { class: "num", "data-run-duration": duration,
          "data-run-duration-available": Number.isFinite(a.duration_s) ? "1" : "0" },
          durationNode)];
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
    for (const a of sample.algorithms) for (const m of a.metrics) if (m.qc_band)
      banded.push([m.label, rawCell(fmtMetric(m, { algorithmId: a.algorithm_id }), m.value, m.unit,
        `${a.algorithm_id}/${m.metric_id}, QC band`), m.qc_band]);
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
          ? h("p", {}, `Candidate microbial estimate: ${fmt(microbial.microbial_rpm_primary, "estimated reads/million", { unitDisplay: false })} per million primary QC-passing reads; ${fmtCount(microbial.candidate_unmapped_reads)} unmapped reads submitted. Cohort 97.5th percentile: ${microbial.cohort_p975 === null ? "unavailable" : fmt(microbial.cohort_p975, "estimated reads/million", { unitDisplay: false })}${microbial.cohort_p975 === null ? "" : ` (n=${fmtCount(microbial.cohort_n)}, ${microbial.cohort_version})`}.`)
          : "",
        microbial.microbial_rpm_primary !== undefined
          ? h("p", { class: "help" }, `${fmtCount(microbial.too_short_unmapped_reads)} unmapped reads were shorter than the registered Bracken length; ${fmtCount(microbial.missing_sequence_unmapped_reads)} lacked sequence. Neither group was classified. The per-million rate still divides by all primary QC-passing non-duplicate reads.`)
          : "",
        microbial.omitted_taxa ? h("p", { class: "help" }, `${microbial.omitted_taxa} lower-abundance taxa omitted from the bounded display. The headline estimate includes them, so it can exceed the sum of visible rows; absent taxa are not assumed zero in cohort comparisons.`) : "",
        microbial.taxa.length
          ? h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, ...["Candidate taxon", "Group", "Estimate / million", "Cohort 97.5th pct", "Context"].map((label) => h("th", { scope: "col" }, label)))),
            h("tbody", {}, ...microbial.taxa.map((row) => h("tr", {},
              h("th", { scope: "row" }, `${row.name} (taxid ${row.taxid})`),
              h("td", {}, row.category), h("td", {}, fmt(row.rpm_primary, "estimated reads/million", { unitDisplay: false })),
              h("td", {}, row.cohort_p975 === null ? `unavailable (n=${row.cohort_n})` : `${fmt(row.cohort_p975, "estimated reads/million", { unitDisplay: false })} (n=${row.cohort_n})`),
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
            h("td", {}, pair.state === "unavailable" ? "unavailable" : fmtCount(pair.metrics.n)))))))
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
    button.addEventListener("click", () => {
      button.focus();
      dialog.showModal();
    });
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
      const display = duration.querySelector("[data-run-duration-display]");
      if (display) display.textContent = note === "latest run in progress" && duration.dataset.runDurationAvailable === "1"
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
    if (polling || state.sessionExpired) return;
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
      const status = await Promise.race([api("/api/status", controller.signal, { timeoutMs: null }), deadline]);
      const wasDisconnected = state.connectionLost;
      state.missed = 0;
      state.lastOk = new Date();
      state.connectionLost = false;
      state.disconnectedSince = null;
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
          setPollStatus(`A Lab run is in progress (step: ${status.active.step || "starting"}, ${minutes} min). Results below are from completed steps.${priorInterrupted}${logNotice}`,
            status.generation > state.generationAtLoad ? "Reload" : null);
        } else {
          setPollStatus(`Lab ${status.active.command || "work"} is in progress. Results below are from completed steps.${priorInterrupted}${logNotice}`,
            status.generation > state.generationAtLoad ? "Reload" : null);
        }
      } else if (status.interrupted) {
        // A run that died (stale heartbeat, open log) is "interrupted", even
        // if it published results before it stopped.
        setPollStatus(`The last run (${status.interrupted.run_id}) stopped during ${status.interrupted.step || "a step"}. Results shown are from completed steps. Rerun the same command; only missing results recompute.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (logIssue) {
        setPollStatus(`The last run${logIssue.run_id ? ` (${logIssue.run_id})` : ""} status is unavailable (${logReason}). Results shown are from completed steps. Inspect the run log or rerun the command.`,
          status.generation > state.generationAtLoad ? "Reload" : null);
      } else if (status.generation > state.generationAtLoad && previous && previous.active) {
        setPollStatus("New results are available.", "Reload");
      } else if (status.generation > state.generationAtLoad) {
        setPollStatus("Results changed on disk since this page loaded.", "Reload");
      } else {
        setPollStatus("");
      }
      if (status.library && status.library.error) {
        document.getElementById("status").append(h("span", { class: "problem", "data-library-error": "" },
          ` Interpretation library on disk is invalid (${status.library.error}). Showing v${status.library.version}, loaded at ${status.library.loaded_at}.`));
      }
      if (wasDisconnected && state.sample?.found) {
        const refreshed = await refreshSelectedSample();
        if (refreshed.kind !== "ok") setControlsEnabled(false);
      } else setControlsEnabled(!state.resultStale);
      if (!state.resultStale) document.body.classList.remove("dimmed");
      renderRecoveryState();
      updateTrustQcRunStatus(status);
    } catch (error) {
      if (error.session || state.sessionExpired) return;
      state.missed += 1;
      if (state.missed >= MISSED_LIMIT) {
        const since = state.lastOk ? state.lastOk.toTimeString().slice(0, 5) : "page load";
        state.connectionLost = true;
        state.disconnectedSince = since;
        state.resultStale = !!state.sample;
        document.body.classList.add("dimmed");
        restoreRenderedRoute();
        setControlsEnabled(false);
        renderRecoveryState();
      }
    } finally {
      clearTimeout(timeoutId);
      polling = false;
    }
  }

  function isLocalRecordedRoute(hash) {
    if (state.connectionLost || state.sessionExpired || !state.sample?.found || typeof hash !== "string") return false;
    const match = hash.match(/^#\/sample\/([0-9a-f]{12,64})\/([a-z-]+)(?:\?.*)?$/);
    return !!match && (match[1] === state.sample.id12 || match[1] === state.sample.source_id) &&
      LENSES.some((lens) => lens.id === match[2]);
  }

  // Keep the recorded view readable while blocking requests for new results.
  function setControlsEnabled(enabled) {
    setFetchControlEnabled(document.getElementById("sample-switcher"), enabled);
    for (const control of document.querySelectorAll("button[data-requires-server], select[data-requires-server]")) {
      if (control.id === "readiness-refresh" && !state.connectionLost && !state.sessionExpired) {
        setFetchControlEnabled(control, true);
        if (state.selectedRefresh) control.disabled = true;
        continue;
      }
      setFetchControlEnabled(control, enabled);
    }
    for (const link of document.querySelectorAll("a[href^='#/']")) {
      setLinkEnabled(link, enabled || isLocalRecordedRoute(link.getAttribute("href")));
    }
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
    if (event.target.closest("#readiness-refresh") && !state.connectionLost && !state.sessionExpired) return;
    const link = event.target.closest("a[href^='#/']");
    if (link && isLocalRecordedRoute(link.getAttribute("href"))) return;
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
    if (isLocalRecordedRoute(location.hash)) return;
    event.stopImmediatePropagation();
    restoreRenderedRoute();
  }, true);

  // -- start ----------------------------------------------------------------------------
  async function start() {
    setupKey();
    setupLensNavigation();
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
      state.rootHasRuns = !!listing.root_has_runs;
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
      state.initialLoadFailure = true;
      if (error.session || error.message === "bootstrap") expireSession();
      const main = document.getElementById("main");
      main.replaceChildren(h("h1", { class: "view-title" }, "Traceback Lab"),
        h("p", { class: "problem" }, error.session || error.message === "bootstrap"
          ? "This page needs the one-use link printed by `traceback lab serve`. Press Enter in that terminal for a fresh link."
          : "The Lab results could not be loaded. Check the server, then retry with the current selection."));
      renderSampleContext();
      renderRecoveryState();
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
