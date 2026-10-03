(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const status = byId("status");
  const jobs = byId("jobs");
  const left = byId("left-result");
  const right = byId("right-result");
  const exactRows = byId("exact-rows");
  const panels = byId("panels");
  const provenance = byId("provenance");
  const compatibility = byId("compatibility");
  const renderStartedAt = performance.now();
  window.__tracebackRenderMetrics = { startedAtMs: renderStartedAt, readyAtMs: null, durationMs: null };

  const requestJson = async (path) => {
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) throw new Error("Local result data unavailable");
    return response.json();
  };
  const countText = (count) => {
    if (!count || count.state !== "observed") return count ? count.state : "missing";
    return String(count.value);
  };
  const addCell = (row, value) => {
    const cell = document.createElement("td");
    cell.textContent = value;
    row.append(cell);
  };
  const displayValue = (value) => {
    if (value === null || value === undefined) return "missing";
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  };
  const renderArtifact = (article, label, value) => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = label;
    const pre = document.createElement("pre");
    pre.textContent = displayValue(value);
    details.append(summary, pre);
    article.append(details);
  };
  const renderDocument = (label, documentPayload) => {
    const model = documentPayload.models;
    const row = model.result_view.rows.find((item) => item.result_identity.result_id === model.catalog_ref.result_id);
    const article = document.createElement("article");
    const title = document.createElement("h3");
    title.textContent = `${label}: ${model.catalog_ref.method_ref.method_id} ${model.catalog_ref.method_ref.version}`;
    const state = document.createElement("p");
    state.textContent = `${row.execution_state}; ${row.information_state}; ${row.trust_state}; ${row.qualification_state}`;
    const gate = document.createElement("p");
    gate.className = documentPayload.eligibility.release_explorer_allowed ? "release-on" : "release-off";
    gate.textContent = documentPayload.eligibility.release_explorer_allowed
      ? "Release explorer eligible"
      : "Research inspection only; release explorer and export are disabled";
    article.append(title, state, gate);
    renderArtifact(article, "E07 fragment values, units, uncertainty, and missingness", model.fragment);
    renderArtifact(article, "E08 cell-origin values, units, uncertainty, and missingness", model.cell_origin);
    renderArtifact(article, "E09 CNA values, units, uncertainty, and missingness", model.cna);
    renderArtifact(article, "E11 provenance identities and differences", model.provenance);
    renderArtifact(article, "E13 sensitivity values, units, uncertainty, and missingness", model.sensitivity);
    renderArtifact(article, "E10 portable exact tables", model.portable);
    renderArtifact(article, "E12 longitudinal", model.longitudinal_state);
    panels.append(article);
    const tableRow = document.createElement("tr");
    const heading = document.createElement("th");
    heading.scope = "row";
    heading.textContent = label;
    tableRow.append(heading);
    addCell(tableRow, row.accessible_label);
    addCell(tableRow, model.result_view.surface_state);
    addCell(tableRow, countText(row.denominator.displayed_records));
    addCell(tableRow, countText(row.denominator.eligible_records));
    addCell(tableRow, row.qc_label);
    exactRows.append(tableRow);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${label} exact identities and attrition`;
    const list = document.createElement("dl");
    const values = [
      ["Result ID", model.catalog_ref.result_id],
      ["Bundle digest", model.catalog_ref.bundle_sha256],
      ["Method definition", model.catalog_ref.method_definition_sha256],
      ["Authority head", model.catalog_ref.authority_head_sha256],
      ["Filter digest", model.result_view.filters_sha256],
      ["Attrition reasons", row.denominator.attrition.map((item) => `${item.accessible_label}: ${countText(item.count)}`).join("; ")],
    ];
    values.forEach(([term, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = term;
      dd.textContent = value;
      list.append(dt, dd);
    });
    details.append(summary, list);
    provenance.append(details);
    return model;
  };
  const renderSelection = async () => {
    panels.replaceChildren();
    exactRows.replaceChildren();
    provenance.replaceChildren();
    const ids = [left.value, right.value];
    if (ids.some((id) => !id)) return;
    try {
      const documents = await Promise.all(ids.map((id) => requestJson(`/api/v1/explorer/results/${id}`)));
      documents.forEach((item, index) => renderDocument(index === 0 ? "A" : "B", item));
      if (ids[0] === ids[1]) {
        compatibility.textContent = "unknown; comparison blocked: select two distinct results";
        compatibility.dataset.comparisonState = "unknown";
      } else {
        const query = new URLSearchParams({ left: ids[0], right: ids[1] });
        const comparison = await requestJson(`/api/v1/explorer/compare?${query}`);
        compatibility.textContent = comparison.synchronized
          ? `${comparison.outcome}; exact pair and filter context synchronized${comparison.delta_available ? "; deltas available" : "; no deltas"}`
          : `${comparison.outcome}; comparison blocked: ${comparison.blocked_reason}`;
        compatibility.dataset.comparisonState = comparison.outcome;
      }
      document.documentElement.dataset.renderState = "ready";
      const readyAtMs = performance.now();
      window.__tracebackRenderMetrics.readyAtMs = readyAtMs;
      window.__tracebackRenderMetrics.durationMs = readyAtMs - renderStartedAt;
    } catch (_) {
      panels.replaceChildren();
      exactRows.replaceChildren();
      provenance.replaceChildren();
      compatibility.textContent = "Selected view unavailable";
    }
  };
  const loadCatalog = async () => {
    const params = new URLSearchParams();
    const methodId = byId("method-id").value.trim();
    const methodVersion = byId("method-version").value.trim();
    if (methodId || methodVersion) {
      if (!methodId || !methodVersion) throw new Error("Method ID and version are both required");
      params.set("method_id", methodId);
      params.set("method_version", methodVersion);
    }
    const payload = await requestJson(`/api/v1/explorer/catalog?${params}`);
    const registered = payload.results.filter((item) => item.has_registered_view && item.eligibility.research_inspection_allowed);
    const options = registered.map((item) => {
      const option = document.createElement("option");
      option.value = item.ref.result_id;
      option.textContent = `${item.ref.method_ref.method_id} ${item.ref.method_ref.version} — ${item.ref.result_id.slice(0, 15)}`;
      return option;
    });
    left.replaceChildren(...options.map((item) => item.cloneNode(true)));
    right.replaceChildren(...options.map((item) => item.cloneNode(true)));
    if (right.options.length > 1) right.selectedIndex = 1;
    status.textContent = registered.length ? `${registered.length} local result views ready` : "No registered result views match the active filters";
    if (!registered.length) {
      document.documentElement.dataset.renderState = "empty";
      const readyAtMs = performance.now();
      window.__tracebackRenderMetrics.readyAtMs = readyAtMs;
      window.__tracebackRenderMetrics.durationMs = readyAtMs - renderStartedAt;
    }
    await renderSelection();
  };
  const renderJobs = async () => {
    const payload = await requestJson("/api/v1/jobs");
    jobs.replaceChildren(...payload.jobs.map((job) => {
      const item = document.createElement("li");
      item.textContent = `${job.headline}: ${job.stage_label}`;
      return item;
    }));
  };
  byId("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadCatalog().catch((error) => { status.textContent = error.message; });
  });
  left.addEventListener("change", renderSelection);
  right.addEventListener("change", renderSelection);
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrap = fragment.get("bootstrap");
  const readerLaunch = fragment.get("reader_launch");
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  // The reader launch credential stays in page memory only; it is sent once,
  // in a same-origin POST body carrying the session cookie, Origin and CSRF.
  const exchangeReaderLaunch = async (csrfToken) => {
    const response = await fetch("/api/v1/session/reader-launch", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Traceback-CSRF": csrfToken },
      body: JSON.stringify({ launch: readerLaunch }),
    });
    document.documentElement.dataset.readerSession = response.ok ? "bound" : "denied";
    status.textContent = response.ok
      ? "Longitudinal reader session bound"
      : "Longitudinal reader launch was denied; ask the operator for a new link";
    if (response.ok) {
      // The E12 view (longitudinal.js) keeps the CSRF token in page memory only.
      window.dispatchEvent(new CustomEvent("traceback:longitudinal", { detail: { csrfToken } }));
    }
    return response.ok;
  };
  // Explorer and jobs routes are operator-only (H1): a reader session never
  // requests them, and their sections are hidden so the longitudinal view is
  // what a reader sees.
  const OPERATOR_SECTIONS = ["explorer-filters", "results", "explorer-provenance", "operator-jobs"];
  const enterReaderView = () => {
    document.documentElement.dataset.sessionKind = "reader";
    OPERATOR_SECTIONS.forEach((id) => {
      const section = byId(id);
      if (section) section.hidden = true;
    });
  };
  if (!bootstrap) {
    status.textContent = "Relaunch from the local Traceback command";
    return;
  }
  fetch("/api/v1/session/bootstrap", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bootstrap }),
  }).then(async (response) => {
    if (!response.ok) {
      if (readerLaunch) {
        // A newer link replaces an unused one, and links expire after 60 s.
        status.textContent = response.status === 429
          ? "Too many attempts; wait a minute, then open a new link from the operator"
          : "This link expired or was replaced by a newer one; ask the operator for a new link";
        document.documentElement.dataset.readerSession = "link-unavailable";
        return;
      }
      throw new Error("Local session unavailable");
    }
    const session = await response.json();
    // Branch on the kind the server assigned at exchange, never on the
    // fragment: a reader bootstrap is a reader session from birth.
    if (session.session_kind === "reader") {
      // Bind, then show only the longitudinal view.  A denied launch keeps
      // its message.
      enterReaderView();
      if (!readerLaunch) {
        document.documentElement.dataset.readerSession = "denied";
        status.textContent = "This reader link is incomplete; ask the operator for a new link";
        return;
      }
      await exchangeReaderLaunch(session.csrf_token);
      return;
    }
    if (session.session_kind !== "operator") throw new Error("Local session unavailable");
    document.documentElement.dataset.sessionKind = "operator";
    await renderJobs();
    try {
      await loadCatalog();
    } catch (_) {
      status.textContent = "Local session ready; result explorer unavailable";
    }
  }).catch(() => {
    status.textContent = "Local session unavailable; relaunch Traceback";
  });
})();
