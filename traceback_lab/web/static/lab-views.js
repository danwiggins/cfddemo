// Traceback Lab views: landing, Findings, Stability, #/about/clia (slice S6c).
// Same rules as lab.js: no innerHTML, no inline style attributes (CSSOM only),
// every chart has an aria-label and an adjacent data table.
"use strict";

(function () {
  const L = window.TracebackLab;
  const { h, fmt, fmtMetric, fmtMetricValue, fmtCount, rawCell, dataTable, chart } = L;
  const SVG = "http://www.w3.org/2000/svg";
  const STATE = {
    in_range: ["●", "in range"],
    above_typical: ["▲", "above typical"],
    markedly_above: ["▲▲", "markedly above"],
    below_typical: ["▼", "below typical"],
    markedly_below: ["▼▼", "markedly below"],
    not_interpretable: ["▨", "not interpretable"],
    no_reference: ["○", "no reference"],
  };
  const CELL = {
    ok_usable: ["●", "usable"],
    ok_unvalidated: ["?", "stability not validated"],
    ok_unstable: ["◐", "unstable"],
    not_interpretable: ["▨", "not interpretable"],
    skipped: ["○", "skipped"],
    failed: ["✕", "failed"],
    unavailable: ["?", "result unavailable"],
  };
  const cellText = (state, reason, fallback = "no result") => {
    const text = (CELL[state] || ["", state || fallback])[1];
    return reason ? `${text} (${reason})` : text;
  };
  const chartPercent = (fraction) => Number((100 * fraction).toPrecision(3));
  const FLAG_TEXT = {
    CLIA: "FLAG: CLIA — health-assessment labels on an individual sample",
    FDA: "FLAG: FDA — cancer-detection claim if marketed.",
    PRIVACY: "FLAG: PRIVACY — can reveal pregnancy or transplant status.",
    LEGAL: "FLAG: LEGAL — possible method patents; no FTO review.",
    PII: "FLAG: PII — file paths often embed donor names.",
  };

  // Register the built lenses in the fixed order (unbuilt lenses stay absent).
  L.LENSES.splice(0, L.LENSES.length,
    { id: "findings", label: "Findings", count: (s) => ` (${outOfRange(s).length})` },
    { id: "trust-qc", label: "Trust/QC" },
    { id: "stability", label: "Stability" },
    { id: "signals", label: "Signals" },
    { id: "body-map", label: "Body map" },
    { id: "agreement", label: "Agreement" },
    { id: "compare", label: "Compare" },
    { id: "evidence", label: "Evidence" },
    { id: "methods", label: "Methods" },
  );

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
    for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }

  function flagPill(type, text) { return h("span", { class: "flag-pill" }, text || FLAG_TEXT[type] || `FLAG: ${type}`); }
  function metricFlag(metric, type) {
    return type === "PRIVACY" && metric.algorithm_id === "viral-reads"
      ? flagPill(type, "FLAG: PRIVACY — viral-read signals can reveal sensitive infection-related information.")
      : flagPill(type);
  }
  function tierBadge(tier) { return tier ? h("span", { class: `tier tier-${tier}` }, tier) : h("span", { class: "tier" }, "—"); }
  function stateReason(state, reason, fallback) {
    return state !== "failed" && typeof reason === "string" && reason
      ? `${fallback} (${reason.replace(/_/g, " ")})` : fallback;
  }
  function stateChip(metric) {
    const [glyph, text] = STATE[metric.state] || ["", metric.state || "—"];
    const reason = metric.reason ? ` (${metric.reason.replace(/_/g, " ")})` : "";
    return h("span", { class: "state-chip" }, h("span", { "aria-hidden": "true" }, `${glyph} `), text + reason,
      metric.exploratory_n ? h("em", {}, ` exploratory (n = ${metric.exploratory_n})`) : "");
  }

  // Inline markdown (bold, links, [[FLAG pills]]) without innerHTML.
  function inline(text) {
    const out = [];
    const pattern = /\*\*(.+?)\*\*|\[\[(.+?)\]\]|\[([^\]]+)\]\((https?:[^)]+)\)/g;
    let last = 0;
    let m;
    while ((m = pattern.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1]) out.push(h("strong", {}, m[1]));
      else if (m[2]) out.push(h("span", { class: "flag-pill" }, m[2]));
      else out.push(h("a", { href: m[4], rel: "noreferrer noopener", target: "_blank" }, m[3]));
      last = pattern.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function paragraphs(markdown) { return markdown.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean).map((p) => h("p", {}, ...inline(p))); }

  let copy = null;
  async function cliaCopy() {
    if (!copy) copy = await (await fetch("/assets/copy/clia.json", { credentials: "same-origin" })).json();
    return copy;
  }

  // -- license callouts: one full callout per id per route, pills elsewhere ---------------
  // A pill jumps to the route's full callout without touching the hash route.
  function licenseRef(sample, id) {
    const lic = (sample.licenses || []).find((x) => x.license_id === id);
    return h("button", { type: "button", class: "license-pill", "aria-describedby": `license-${id}`, onclick: () => {
      const target = document.getElementById(`license-${id}`);
      if (target) { target.setAttribute("tabindex", "-1"); target.scrollIntoView({ block: "center" }); target.focus({ preventScroll: true }); }
    } }, `${lic ? lic.headline : id} ↗`);
  }
  function unverifiedBadge(entry) {
    return entry && !entry.citation_checked ? h("span", { class: "unverified" }, "UNVERIFIED citation") : "";
  }
  function spacedUnverifiedBadge(entry) {
    const badge = unverifiedBadge(entry);
    return badge ? [" ", badge] : [];
  }
  function licenseCallouts(sample) {
    const used = sample.licenses || [];
    if (!used.length) return h("section", { class: "panel", "aria-labelledby": "lic-title" }, h("h2", { id: "lic-title" }, "License callouts"), h("p", { class: "empty" }, "No algorithm on this page carries a license callout."));
    return h("section", { class: "panel", "aria-labelledby": "lic-title" }, h("h2", { id: "lic-title" }, "License callouts"),
      ...used.map((lic) => h("div", { class: "license-callout", id: `license-${lic.license_id}` },
        h("h3", {}, lic.headline), h("p", {}, lic.body), h("p", { class: "help" }, h("a", { href: lic.url, rel: "noreferrer noopener", target: "_blank" }, "License or source")))));
  }

  // -- landing ------------------------------------------------------------------------------
  function landingCliaPanel() {
    const panel = h("section", { class: "clia-panel", "aria-labelledby": "clia-title" }, h("h2", { id: "clia-title", class: "clia-title" }, "What CLIA means for this demo"));
    const shortBox = h("div", { class: "clia-short" }, h("p", {}, "Loading…"));
    const fullBox = h("div", { class: "clia-full", id: "clia-full", hidden: true });
    const toggle = h("button", { type: "button", class: "control clia-toggle", "aria-expanded": "false", "aria-controls": "clia-full" }, "Read the full explainer ▾");
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      fullBox.hidden = !open;
      panel.classList.toggle("expanded", open);
      toggle.textContent = open ? "Hide the full explainer ▴" : "Read the full explainer ▾";
    });
    panel.append(shortBox, h("p", { class: "clia-actions" }, toggle, " ", h("a", { href: "#/about/clia" }, "Sources →")), fullBox);
    cliaCopy().then((c) => {
      shortBox.replaceChildren(...paragraphs(c.short));
      fullBox.replaceChildren(...paragraphs(c.full));
      measure();
    });
    return panel;
  }

  function landing(sample) {
    const rows = (sample.algorithms || []).filter((algorithm) => !algorithm.hidden);
    const counts = {
      ok: rows.filter((algorithm) => algorithm.status === "ok").length,
      skipped: rows.filter((algorithm) => algorithm.status === "skipped").length,
      failed: rows.filter((algorithm) => algorithm.status === "failed").length,
      other: rows.filter((algorithm) => !["ok", "skipped", "failed"].includes(algorithm.status)).length,
    };
    const selectionProblems = [
      (sample.unreadable_results || []).length ? `${sample.unreadable_results.length} unreadable` : null,
      (sample.ambiguous_results || []).length ? `${sample.ambiguous_results.length} ambiguous` : null,
      sample.malformed_result_selections ? "malformed selection present" : null,
    ].filter(Boolean).join(" · ");
    const overview = h("section", { class: "panel landing-intro", "aria-labelledby": "landing-intro-title" },
      h("h2", { id: "landing-intro-title" }, "Sample overview"),
      h("p", { class: "landing-identity" }, `Sample ${sample.id12} · `,
        h("span", { class: "sample-context-kind" }, sample.example ? "EXAMPLE (synthetic)" : "Not marked as the built-in synthetic example")),
      h("dl", { class: "landing-summary" },
        h("div", {}, h("dt", {}, "Latest recorded activity"), h("dd", {}, sample.run_utc ? sample.run_utc.slice(0, 10) : "Unavailable")),
        h("div", {}, h("dt", {}, "Approximate depth"), h("dd", {}, sample.depth?.approx_depth_x == null ? "Unavailable" : `${sample.depth.approx_depth_x.toPrecision(3)}x`)),
        h("div", {}, h("dt", {}, "Recorded method states"), h("dd", {}, rows.length
          ? `${rows.length} visible entries: ${counts.ok} OK · ${counts.skipped} skipped · ${counts.failed} failed${counts.other ? ` · ${counts.other} other` : ""}`
          : "No recorded visible method results")),
        selectionProblems ? h("div", {}, h("dt", {}, "Result selection problems"), h("dd", { class: "problem" }, selectionProblems)) : ""),
      h("nav", { class: "landing-actions", "aria-label": "Explore this sample" },
        h("a", { href: `#/sample/${sample.id12}/signals` }, "Explore signals"),
        h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Check reliability"),
        h("a", { href: `#/sample/${sample.id12}/methods` }, "Method readiness")));
    const out = [h("h1", { class: "visually-hidden" }, `Traceback Lab — ${sample.id12}`), overview];
    const sentence = h("p", { class: "headline", id: "headline" }, sample.headline.sentence);
    if (sample.headline.unlock) sentence.append(" ", h("span", { title: "literature, not measured" }, sample.headline.unlock.text), h("span", { class: "visually-hidden" }, " (literature, not measured)"));
    out.push(sentence);
    // DOM order is reading order: defended, curve, controls (CSS places them in two columns).
    const band = h("div", { class: "landing-band" }, defendedBlock(sample), curveBlock(sample), controlsBlock(sample));
    out.push(band, landingCliaPanel(), samplesTable(), licenseCallouts(sample));
    return out;
  }

  function defendedBlock(sample) {
    const block = sample.defended;
    const provisional = sample.excluded_chunks && sample.excluded_chunks.length ? " (provisional)" : "";
    const section = h("section", { class: "panel defended", "aria-labelledby": "defended-title" }, h("h3", { id: "defended-title" }, `Defended results (${block.rows.length})${provisional}`));
    if (!sample.curve.downsampled) {
      section.append(h("p", { class: "empty" }, "Defended results need the depth curve (stability at 0.5×)."));
      return section;
    }
    if (!block.rows.length) {
      const r = block.none_reasons;
      const depth = sample.depth ? `${sample.depth.approx_depth_x.toPrecision(3)}x` : "this depth";
      section.append(h("p", { class: "none-defended" }, `No result is defended at ${depth}.`),
        h("p", {}, `${r.usable_but_speculative} usable but SPECULATIVE · ${r.unstable_at_half} unstable at 0.5× · ${r.citation_unverified} citation unverified · ${r.not_interpretable} not interpretable at this depth`));
      if (sample.headline.unlock) section.append(h("p", { class: "help" }, `${sample.headline.unlock.text} (literature, not measured)`));
      return section;
    }
    const list = h("ul", { class: "defended-rows" });
    for (const row of block.rows) {
      const lens = ["above_typical", "markedly_above", "below_typical", "markedly_below"].includes(row.state) ? "findings" : "stability";
      const algorithm = sample.algorithms.find((candidate) => candidate.algorithm_id === row.algorithm_id);
      const headline = algorithm?.metrics.find((metric) => metric.metric_id === algorithm.headline_metric);
      list.append(h("li", { class: "defended-row" },
        h("a", { href: `#/sample/${sample.id12}/${lens}` },
          h("span", { class: "defended-label" }, row.label), " ",
          h("strong", {}, headline ? fmtMetricValue(headline, row.value, { algorithmId: row.algorithm_id })
            : fmt(row.value, row.unit)), " ",
          stateChip({ state: row.state }), " ", tierBadge(row.tier),
          h("span", { class: "defended-evidence" }, row.evidence)),
        ...row.license_ids.map((id) => licenseRef(sample, id))));
    }
    section.append(list);
    return section;
  }

  function curveBlock(sample) {
    const curve = sample.curve;
    const run = curve.run_depth_x;
    const section = h("section", { class: "panel curve", "aria-labelledby": "curve-title" });
    const heading = h("div", { class: "curve-heading" }, h("h3", { id: "curve-title" }, "Depth → utility"),
      h("span", { class: "legend", "aria-hidden": "true" }, "● usable ? unavailable or stability not validated ◐ unstable ▨ not interpretable ○ skipped ✕ failed ◇ literature"));
    section.append(heading);
    if (!curve.downsampled) {
      section.append(h("p", { class: "empty" }, h("span", {}, "This run used "), h("code", {}, "--no-downsample"), `. Run `, h("code", {}, `traceback lab run ${sample.id12}`), " to draw the curve."));
    }
    const usable = curve.rows.filter((r) => r.full_state === "ok_usable").length;
    const label = `${curve.rows.length} algorithms; ${usable} usable at this run's depth (${run ? run.toPrecision(3) : "?"}x); cells at ${curve.fractions.map((f) => `${f * 100}%`).join(", ")} of the reads.`;
    const lo = Math.log10(Math.max(1e-6, (run || 1) * 0.1));
    const hi = Math.log10(10);
    const pos = (x) => `${Math.min(100, Math.max(0, ((Math.log10(Math.max(x, 1e-9)) - lo) / (hi - lo)) * 100))}%`;
    const grid = h("div", { class: "curve-grid", role: "img", "aria-label": label });
    const axis = h("div", { class: "curve-row curve-axis", "aria-hidden": "true" }, h("span", { class: "curve-label" }, ""));
    const track = h("div", { class: "curve-track" });
    for (const tick of [0.01, 0.1, 1, 10]) {
      if (Math.log10(tick) < lo) continue;
      const t = h("span", { class: "curve-tick" }, `${tick}x`); t.style.left = pos(tick); track.append(t);
    }
    if (run) { const rule = h("span", { class: "curve-run" }, `this run ${run.toPrecision(3)}x`); rule.style.left = pos(run); track.append(rule); }
    axis.append(track);
    grid.append(axis);
    for (const row of curve.rows) {
      const line = h("div", { class: "curve-row" });
      // One line per row (a fixed 44 px target) so the fold budget holds with any font; full label in title + data table.
      const name = h("button", { type: "button", class: "curve-label", "aria-expanded": "false", title: row.input_gate ? `${row.label}: ${row.input_gate}` : row.label }, row.label);
      const cells = h("div", { class: "curve-track" });
      for (const cell of row.cells) {
        const [glyph, text] = CELL[cell.state] || ["·", "no result"];
        const description = stateReason(cell.state, cell.reason, text);
        const mark = h("span", { class: `curve-cell cell-${cell.state || "none"}`, title: `${cell.label}: ${description}` }, h("span", { "aria-hidden": "true" }, glyph), h("span", { class: "visually-hidden" }, `${cell.label} ${description}`));
        if (cell.depth_x) mark.style.left = pos(cell.depth_x);
        cells.append(mark);
      }
      if (row.min_depth_x && (!run || row.min_depth_x > run)) {
        const lit = h("span", { class: "curve-lit", title: "literature, not measured" }, row.min_depth_x > 10 ? "≥10x →" : "◇ lit.");
        lit.style.left = pos(Math.min(row.min_depth_x, 10));
        cells.append(lit);
      }
      const secondary = (sample.stability || []).filter((p) => p.algorithm_id === row.algorithm_id && p.metric_id !== row.metric_id);
      const detail = h("div", { class: "curve-detail", hidden: true },
        h("p", {}, `${row.label}: full-depth state ${(CELL[row.full_state] || ["", row.full_state])[1]}; tier ${row.tier}; literature minimum ${row.min_depth_x ? row.min_depth_x + "x (literature, not measured)" : "none"}${row.input_gate ? `; input gate ${row.input_gate}` : ""}.`),
        secondary.length ? h("table", { class: "data-table" }, h("caption", {}, "Secondary metrics by fraction"),
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Metric"), ...curve.fractions.map((f) => h("th", { scope: "col" }, `${f * 100}%`)))),
          h("tbody", {}, ...secondary.map((p) => h("tr", {}, h("th", { scope: "row" }, p.label), ...curve.fractions.map((f) => {
            const pt = p.points.find((q) => q.fraction === f);
            return h("td", {}, pt ? cellText(pt.state, pt.reason, "—") : "—");
          }))))) : "");
      name.addEventListener("click", () => { const open = name.getAttribute("aria-expanded") !== "true"; name.setAttribute("aria-expanded", String(open)); detail.hidden = !open; });
      line.append(name, cells);
      grid.append(line, detail);
    }
    section.append(grid);
    section.append(dataTable("Depth curve cells (literature minimums are not measured)", ["Algorithm", ...curve.fractions.map((f) => `${f * 100}%`), "Literature minimum", "Input gate"],
      curve.rows.map((row) => [row.label, ...row.cells.map((c) => stateReason(c.state, c.reason, (CELL[c.state] || ["", "—"])[1])), row.min_depth_x ? `${row.min_depth_x}x (literature, not measured)` : "—", row.input_gate || "—"])));
    return section;
  }

  function controlsBlock(sample) {
    const section = h("section", { class: "panel controls", "aria-labelledby": "controls-title" }, h("h3", { id: "controls-title" }, "Positive controls"));
    // SYNTHETIC
    const syn = h("div", { class: "control-block" }, h("span", { class: "control-badge" }, "SYNTHETIC"), " known answer");
    const rows = sample.synthetic || [];
    if (!rows.length) {
      syn.append(h("p", { class: "empty" }, sample.example ? "Not in the synthetic example: copy-number needs hg38 assets." : `No synthetic control yet. Run traceback lab run ${sample.id12}.`));
    } else {
      const points = rows.map((r) => [chartPercent(r.tf_expected),
        r.tf_recovered === null ? null : chartPercent(r.tf_recovered)]);
      const recovered = rows.filter((r) => r.recovered).map((r) => fmt(r.tf_expected, "fraction"));
      const missed = rows.filter((r) => !r.recovered && r.tf_expected >= 0.1).map((r) => fmt(r.tf_expected, "fraction"));
      const tolerance = fmt(0.05, "fraction", { role: "difference" });
      const caption = recovered.length && !missed.length
        ? `Recovered within ±${tolerance} at ${recovered.join(" and ")}`
        : `Recovered within ±${tolerance} at ${recovered.length ? recovered.join(" and ") : "none"}; not recovered at ${missed.join(" and ") || "—"} (this depth: ${fmt(sample.depth?.approx_depth_x, "x")})`;
      syn.append(chart(`Synthetic CNA spike: expected against recovered tumour fraction, both in %. ${caption}.`, {
        grid: { left: 50, right: 16, top: 16, bottom: 40 },
        xAxis: { type: "value", name: "expected TF (%)", min: 0, max: 35 },
        yAxis: { type: "value", name: "recovered TF (%)", min: 0, max: 35 },
        tooltip: { trigger: "item", renderMode: "richText", formatter: (point) =>
          `Expected: ${fmt(point.value[0], "%")}\nRecovered: ${fmt(point.value[1], "%")}` },
        series: [
          { type: "line", data: [[0, 5], [30, 35]], lineStyle: { type: "dashed", color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "line", data: [[5, 0], [35, 30]], lineStyle: { type: "dashed", color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "line", data: [[0, 0], [35, 35]], lineStyle: { color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "scatter", data: points.filter((p) => p[1] !== null), symbolSize: 10, itemStyle: { color: "#1f5f8b" } },
        ],
      }, dataTable("Synthetic CNA spike", ["Expected TF (%)", "Recovered TF (%)", `Recovered (±${tolerance})`, "Status"],
        rows.map((r) => [fmt(r.tf_expected, "fraction"), fmt(r.tf_recovered, "fraction"), r.recovered, r.status]))), h("p", { class: "help" }, caption));
    }
    // REPEATABILITY
    const rep = h("div", { class: "control-block" }, h("span", { class: "control-badge" }, "REPEATABILITY"), " sampling noise, not a known answer");
    if (sample.repeatability) {
      const r = sample.repeatability;
      rep.append(h("p", {}, `Median split-half |A−B|: ${fmt(r.median_relative_abs_diff * 100, "%")} across ${fmtCount(r.metrics)} comparable metrics; ${fmtCount(r.incomparable_metrics || 0)} metric comparison${r.incomparable_metrics === 1 ? "" : "s"} unavailable; ${fmtCount(r.unstable)} outside tolerance among ${fmtCount(r.verdict_metrics)} with a validated verdict; ${fmtCount(r.no_verdict_metrics)} without a validated verdict.`),
        h("ul", {}, ...r.worst.map((w) => h("li", {}, `${w.label}: ${fmt(w.relative_abs_diff * 100, "%")}${w.stable === false ? " (unstable)" : w.stable === null ? " (no validated verdict)" : ""}`))),
        h("a", { href: `#/sample/${sample.id12}/stability` }, "→ Stability"));
    } else {
      const unavailable = (sample.stability || []).filter((panel) => !panel.split &&
        [panel.split_status?.A?.state, panel.split_status?.B?.state].some((state) => state && state !== "not_recorded")).length;
      rep.append(unavailable
        ? h("p", { class: "empty" }, `${unavailable} split metric comparison${unavailable === 1 ? "" : "s"} unavailable; no comparable pair is available. `,
          h("a", { href: `#/sample/${sample.id12}/stability` }, "See Stability"))
        : h("p", { class: "empty" }, `Run traceback lab run ${sample.id12}.`));
    }
    const fullUnavailable = (sample.stability_unavailable || []).length;
    if (fullUnavailable) rep.append(h("p", { class: "problem" },
      `${fullUnavailable} full-depth algorithm result${fullUnavailable === 1 ? "" : "s"} unavailable; `,
      h("a", { href: `#/sample/${sample.id12}/stability` }, "See Stability")));
    const known = h("div", { class: "control-block" }, h("span", { class: "control-badge" }, "KNOWN-STATUS"), " public sample",
      h("p", { class: "empty" }, "Not loaded. Needs public data access. ", flagPill("LEGAL", "FLAG: LEGAL — public data-use terms (EGA controlled access).")));
    section.append(syn, rep, known);
    return section;
  }

  function samplesTable() {
    const rows = L.state.samples;
    const table = h("table", {}, h("caption", {}, "All samples (by run date, newest first)"),
      h("thead", {}, h("tr", {}, ...["ID", "Run date", "Approx depth", "OK / skipped / failed", "Outside a reference", "Controls", ""].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ...rows.map((row) => h("tr", {},
        h("th", { scope: "row" }, h("a", { href: `#/sample/${row.id12}/findings` }, row.id12), row.example ? h("span", { class: "flag-pill example-pill" }, " EXAMPLE (synthetic)") : "",
          row.state === "completed_input_unavailable" ? h("span", { class: "problem" }, " completed input unavailable") : "",
          row.state === "completion_history_unavailable" ? h("span", { class: "problem" }, " completion history unavailable") : "",
          row.unreadable_results && row.unreadable_results.length ? h("span", { class: "problem" }, ` result unreadable (sample ${row.id12})`) : "",
          row.ambiguous_results && row.ambiguous_results.length ? h("span", { class: "problem" }, ` result selection ambiguous (sample ${row.id12})`) : "",
          row.malformed_result_selections ? h("span", { class: "problem" }, ` malformed result selection (sample ${row.id12})`) : ""),
        h("td", {}, row.run_utc ? row.run_utc.slice(0, 10) : "—"),
        h("td", { class: "num" }, row.depth_x !== null ? `${row.depth_x.toPrecision(3)}x` : "—"),
        h("td", {}, `${row.counts.ok} / ${row.counts.skipped} / ${row.counts.failed}`),
        h("td", { class: "num" }, String(row.outside_reference),
          ["cohort_unavailable", "target_unavailable", "stale_target"].includes(row.cohort_state)
            ? h("span", { class: "problem" }, ` · cohort ${row.cohort_state.replace(/_/g, " ")}`) : ""),
        h("td", {}, ["completed_input_unavailable", "completion_history_unavailable"].includes(row.state) ? "unavailable"
          : row.controls.spike === null ? "spike not run" : row.controls.spike ? "spike recovered ✓" : "spike not recovered"),
        h("td", {}, h("a", { href: `#/?sample=${row.id12}` }, "Show on landing"))))));
    return h("section", { class: "panel", "aria-labelledby": "samples-title" }, h("h2", { id: "samples-title" }, "Samples"), h("div", { class: "table-wrap" }, table));
  }

  // Layout facts for the pixel-budget test (acceptance #6), read from the DOM dump.
  function measure() {
    const rect = (el) => (el ? el.getBoundingClientRect() : null);
    const body = document.body.dataset;
    const banner = rect(document.getElementById("banner"));
    const header = rect(document.querySelector(".app-header"));
    const panel = document.querySelector(".clia-panel");
    const viewport = window.innerHeight;
    body.viewportHeight = String(viewport);
    body.bannerHeight = banner ? Math.round(banner.height) : "";
    body.headerHeight = header ? Math.round(header.height) : "";
    if (panel) {
      body.cliaHeight = Math.round(rect(panel).height);
      body.cliaClipped = String(panel.scrollHeight > panel.clientHeight + 1);
    }
    const headline = rect(document.getElementById("headline"));
    body.headlineVisible = String(Boolean(headline && headline.bottom <= viewport));
    const visible = (selector) => [...document.querySelectorAll(selector)].filter((el) => rect(el).bottom <= viewport).length;
    body.defendedVisible = String(visible(".defended-row") + visible(".none-defended"));
    body.defendedTotal = String(document.querySelectorAll(".defended-row").length);
    body.noneDefended = String(Boolean(document.querySelector(".none-defended")));
    if (panel) body.cliaFullyVisible = String(rect(panel).top >= 0 && rect(panel).bottom <= viewport);
    body.curveRowsVisible = String(visible(".curve-grid > .curve-row:not(.curve-axis)"));
    body.pageScrollX = String(document.documentElement.scrollWidth > document.documentElement.clientWidth);
    body.measured = "1";
  }

  // -- Findings -----------------------------------------------------------------------------
  function allMetrics(sample) {
    const out = [];
    for (const a of sample.algorithms) {
      if (a.hidden || a.status !== "ok") continue;
      for (const m of a.metrics) if (typeof m.value !== "object" || m.value === null) out.push(Object.assign({ algorithm_id: a.algorithm_id }, m));
    }
    return out;
  }
  function outOfRange(sample) { return allMetrics(sample).filter((m) => ["above_typical", "markedly_above", "below_typical", "markedly_below"].includes(m.state)); }

  function cohortReadiness(sample, metrics) {
    if (sample.example || sample.cohort_state === "example")
      return "EXAMPLE sample; no kit cohort reference. Run a real sample for cohort comparison";
    if (["cohort_unavailable", "target_unavailable", "stale_target"].includes(sample.cohort_state))
      return `cohort ${sample.cohort_state.replace(/_/g, " ")}; ${sample.cohort_problem?.fix || "reload the sample and check the cohort"}`;
    const cohort = metrics.filter((m) => ["internal_cohort", "provisional_external"].includes(m.reference_kind));
    const eligible = cohort.filter((m) => Number.isInteger(m.eligible_n)).map((m) => m.eligible_n);
    const stale = cohort.some((m) => m.cohort_target_state === "stale_parameters" || m.cohort_target_state === "missing_selected_result");
    const excluded = {};
    for (const metric of cohort) for (const [reason, count] of Object.entries(metric.cohort_excluded || {})) {
      if (Number.isInteger(count) && count > 0) excluded[reason] = Math.max(excluded[reason] || 0, count);
    }
    const leaveOneOut = Boolean(excluded.self);
    delete excluded.self;
    const reasons = Object.entries(excluded).map(([reason, count]) =>
      `${reason === "incompatible" ? "protocol incompatible" : reason.replace(/_/g, " ")} ${cohort.length > 1 ? `up to ${count} per metric` : count}`);
    const detail = reasons.length ? `; exclusions: ${reasons.join(" · ")}` : "";
    const needsMembers = !eligible.length || Math.min(...eligible) < (sample.cohort_min || 10);
    const remedy = stale ? "; selected result identity changed—reload or recompute this sample, then rebuild the cohort if needed"
      : excluded.incompatible && needsMembers ? "; recompute members with matching reference, aligner and counting protocol, then rebuild the cohort"
        : excluded.missing_result || excluded.missing_metric ? "; recompute missing member results, then rebuild the cohort" : "";
    if (!eligible.length) return (sample.cohort_state === "not_built" ? "cohort not built" : "no comparable cohort values for these metrics") + detail + remedy;
    const low = Math.min(...eligible), high = Math.max(...eligible);
    const n = low === high ? String(low) : `${low}–${high} by metric`;
    const minimum = sample.cohort_min || 10;
    const phase = high < minimum ? ` of ${minimum} needed` : low < minimum ? `; some metrics need ${minimum}`
      : cohort.some((m) => m.percentile !== null && m.percentile !== undefined) ? "; percentile reference"
        : cohort.some((m) => m.exploratory_n !== null && m.exploratory_n !== undefined) ? "; exploratory reference"
          : "; metric not interpretable at this depth or QC";
    return `eligible cohort n = ${n}${leaveOneOut ? " after leave-one-out" : ""}${phase}${detail}${remedy}`;
  }

  function strip(metric) {
    const ref = (metric.entry && metric.entry.reference) || {};
    const width = 320, height = 34;
    if (metric.reference_kind === "published_range" && metric.interval) {
      const [lo, hi] = metric.interval;
      const v = Number(metric.value);
      const min = Math.min(lo, v) - (hi - lo) * 0.2, max = Math.max(hi, v) + (hi - lo) * 0.2;
      const x = (t) => 10 + ((t - min) / (max - min || 1)) * (width - 20);
      return h("figure", { class: "strip" }, svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": metric.caption, class: "strip-svg" },
        svg("title", {}, metric.caption),
        svg("line", { x1: 10, x2: width - 10, y1: 17, y2: 17, class: "strip-track" }),
        svg("rect", { x: x(lo), y: 10, width: Math.max(2, x(hi) - x(lo)), height: 14, class: "strip-band" }),
        // Outside the range the track continues past a break mark (⫽).
        v > hi ? svg("text", { x: (x(hi) + x(v)) / 2, y: 22, class: "strip-break", "text-anchor": "middle" }, "⫽") : "",
        v < lo ? svg("text", { x: (x(lo) + x(v)) / 2, y: 22, class: "strip-break", "text-anchor": "middle" }, "⫽") : "",
        svg("text", { x: x(v), y: 22, class: "strip-mark", "text-anchor": "middle" }, "◆")),
        dataTable(`${metric.label}: published reference strip`, ["Mark", "Value"],
          [["Reference low", rawCell(fmtMetricValue(metric, lo), lo, metric.unit)],
            ["Reference high", rawCell(fmtMetricValue(metric, hi), hi, metric.unit)],
            ["This sample", rawCell(fmtMetric(metric), metric.value, metric.unit)]]),
        h("figcaption", {}, metric.caption));
    }
    if (metric.exploratory_n && metric.interval) {
      // Variant E: the cohort min–max bracket with this sample as ◆ (the dot plot
      // of every member arrives with the cohort store, L1.6).
      const [lo, hi] = metric.interval;
      const v = Number(metric.value);
      const min = Math.min(lo, v), max = Math.max(hi, v);
      const x = (t) => 10 + ((t - min) / (max - min || 1)) * (width - 20);
      return h("figure", { class: "strip" }, svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": metric.caption, class: "strip-svg" },
        svg("title", {}, metric.caption),
        svg("path", { d: `M${x(lo)},8 v18 M${x(lo)},17 H${x(hi)} M${x(hi)},8 v18`, class: "strip-track" }),
        ...(metric.cohort_values || []).map((value) => svg("circle", { cx: x(value), cy: 17, r: 2.5, class: "strip-member" })),
        svg("text", { x: x(v), y: 22, class: "strip-mark", "text-anchor": "middle" }, "◆")),
        dataTable(`${metric.label}: exploratory cohort strip`, ["Mark", "Value"],
          [["Cohort minimum", rawCell(fmtMetricValue(metric, lo), lo, metric.unit)],
            ["Cohort maximum", rawCell(fmtMetricValue(metric, hi), hi, metric.unit)],
            ...(metric.cohort_values || []).map((value, index) =>
              [`Cohort member ${index + 1}`, rawCell(fmtMetricValue(metric, value), value, metric.unit)]),
            ["This sample", rawCell(fmtMetric(metric), metric.value, metric.unit)]]),
        h("figcaption", {}, metric.caption));
    }
    if (metric.percentile !== null && metric.percentile !== undefined) {
      const x = (p) => 10 + (p / 100) * (width - 20);
      const interval = Array.isArray(metric.interval) && metric.interval.length === 2 &&
        metric.interval.every(Number.isFinite) ? metric.interval : null;
      // Give the cutoff CI its own scale; an extreme sample must not compress
      // the whisker to an unreadable stroke. Off-scale samples sit at an edge.
      const low = interval ? Math.min(...interval) : 0;
      const high = interval ? Math.max(...interval) : 1;
      const valueX = (v) => 110 + ((v - low) / (high - low || 1)) * 200;
      const sampleValue = Number(metric.value);
      const sampleOffScale = sampleValue < low ? "left" : sampleValue > high ? "right" : null;
      return h("figure", { class: "strip" }, svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": metric.caption, class: "strip-svg" },
        svg("title", {}, metric.caption),
        svg("rect", { x: x(5), y: 10, width: x(95) - x(5), height: 14, class: "strip-band" }),
        ...[1, 5, 95, 99].map((p) => svg("line", { x1: x(p), x2: x(p), y1: 6, y2: 28, class: "strip-tick" })),
        svg("text", { x: x(metric.percentile), y: 22, class: "strip-mark", "text-anchor": "middle" }, "◆")),
        dataTable(`${metric.label}: percentile strip`, ["Mark", "Percentile (%)"],
          [["Reference band low", 5], ["Reference band high", 95],
            ...[1, 5, 95, 99].map((p) => [`Tick ${p}`, p]), ["This sample", metric.percentile]]),
        interval ? svg("svg", { viewBox: "0 0 320 52", role: "img",
          "aria-label": `95th-percentile cutoff: 90% bootstrap interval ${fmtMetricValue(metric, interval[0])} to ${fmtMetricValue(metric, interval[1])}; separate value scale`, class: "strip-svg cutoff-whisker" },
          svg("title", {}, "95th-percentile cutoff bootstrap interval on the metric value scale"),
          svg("text", { x: 6, y: 17, class: "axis" }, "Cutoff CI"),
          svg("line", { x1: valueX(interval[0]), x2: valueX(interval[1]), y1: 14, y2: 14, class: "cohort-band" }),
          ...interval.map((end) => svg("line", { x1: valueX(end), x2: valueX(end), y1: 8, y2: 20, class: "cohort-band" })),
          svg("text", { x: 6, y: 43, class: "axis" }, "This sample"),
          svg("text", { x: sampleOffScale === "left" ? 110 : sampleOffScale === "right" ? 310 : valueX(sampleValue), y: 43, "text-anchor": "middle", class: "strip-mark" },
            sampleOffScale === "left" ? "◀" : sampleOffScale === "right" ? "▶" : "◆")) : "",
        interval ? dataTable(`${metric.label}: 95th-percentile cutoff`, ["Mark", "Value", "Display"],
          [["Cutoff interval low", rawCell(fmtMetricValue(metric, interval[0]), interval[0], metric.unit), "On scale"],
            ["Cutoff interval high", rawCell(fmtMetricValue(metric, interval[1]), interval[1], metric.unit), "On scale"],
            ["This sample", rawCell(fmtMetric(metric), metric.value, metric.unit), sampleOffScale ? `Off scale ${sampleOffScale}` : "On scale"]]) : "",
        h("figcaption", {}, metric.caption, interval
          ? ` Cutoff CI: ${fmtMetricValue(metric, interval[0])}–${fmtMetricValue(metric, interval[1])} on a separate value scale.${sampleOffScale ? ` This sample is outside the cutoff CI scale (${sampleOffScale}).` : ""}` : ""));
    }
    return h("p", { class: "help" }, metric.caption || (ref.kind === "none" ? "No published reference." : ""));
  }

  function card(sample, metric, seen, open) {
    const entry = metric.entry || {};
    const perf = entry.performance;
    const details = h("div", { class: "card-details", id: `card-${metric.algorithm_id}-${metric.metric_id}`, hidden: !open });
    const toggle = h("button", { type: "button", class: "control", "aria-expanded": String(open), "aria-controls": details.id }, "Details ▾");
    toggle.addEventListener("click", () => { const o = toggle.getAttribute("aria-expanded") !== "true"; toggle.setAttribute("aria-expanded", String(o)); details.hidden = !o; });
    const meaning = ["above_typical", "markedly_above"].includes(metric.state) ? entry.meaning_high : entry.meaning_low;
    const steps = entry.next_steps || {};
    details.append(
      meaning ? h("p", {}, meaning) : "",
      entry.confounders && entry.confounders.length ? h("p", {}, h("strong", {}, "Confounders: "), entry.confounders.join(", ")) : "",
      h("p", {}, h("strong", {}, "Next steps (analytical): "), (steps.analytical || []).join("; ") || "—", " ",
        flagPill("CLIA", "FLAG: CLIA — reads as clinical advice if a donor or clinician sees it.")),
      steps.clinical_setting ? h("p", {}, h("strong", {}, "Clinical setting: "), steps.clinical_setting, " ", flagPill("CLIA", "FLAG: CLIA — reads as clinical advice if a donor or clinician sees it.")) : "",
      h("p", {}, h("strong", {}, "Citations: "), (entry.citations || []).join(", ") || "none",
        entry.citation_checked ? ` · checked by ${entry.citation_checked.by}, ${entry.citation_checked.date}` : ""),
      h("details", { class: "provenance" }, h("summary", {}, "Provenance stamp"), provenance(sample, metric.algorithm_id)),
    );
    const licenses = (entry.license_ids || []).map((id) => {
      if (!seen.has(id)) { seen.add(id); const lic = (sample.licenses || []).find((x) => x.license_id === id); return lic ? h("div", { class: "license-callout", id: `license-${id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)) : ""; }
      return licenseRef(sample, id);
    });
    return h("article", { class: "finding-card" },
      h("div", { class: "card-head" }, h("h3", {}, metric.label), stateChip(metric), " ", tierBadge(metric.tier)),
      h("p", { class: "card-value" }, h("strong", {}, fmtMetric(metric))),
      strip(metric),
      h("p", {}, "Performance: ", perf ? `${perf.value} (${perf.platform || ""}${perf.depth ? ", " + perf.depth : ""}; ${perf.source || ""})` : (entry.performance_missing_reason || "none published"),
        spacedUnverifiedBadge(metric.entry)),
      meaning ? h("p", {}, meaning.split(". ")[0] + ".") : "",
      h("p", {}, ...(entry.flags || []).map((f) => metricFlag(metric, f)), ...licenses),
      toggle, details);
  }

  function provenance(sample, algorithmId) {
    const a = sample.algorithms.find((x) => x.algorithm_id === algorithmId);
    const p = a && a.provenance ? a.provenance : {};
    return h("dl", {},
      ...["tool", "tool_version", "parameters_sha256", "input_id", "source_id", "reference_id", "timestamp_utc"].flatMap((k) => [h("dt", {}, k), h("dd", {}, p[k] || "—")]),
      h("dt", {}, "interpretation_version"), h("dd", {}, sample.interpretation_version),
      h("dt", {}, "cohort_version"), h("dd", {}, sample.cohort_version || "none"),
      h("dt", {}, ""), h("dd", {}, flagPill("PII", "FLAG: PII — file paths often embed donor names. Hashes only, never paths.")));
  }

  function findings(sample) {
    const metrics = allMetrics(sample);
    const groups = { markedly_above: [], markedly_below: [], above_typical: [], below_typical: [], not_interpretable: [], no_reference: [], in_range: [] };
    for (const m of metrics) (groups[m.state] || groups.no_reference).push(m);
    const tierOrder = { PROVEN: 0, NEWER: 1, SPECULATIVE: 2 };
    for (const key of ["markedly_above", "markedly_below", "above_typical", "below_typical"]) groups[key].sort((a, b) => (tierOrder[a.tier] ?? 9) - (tierOrder[b.tier] ?? 9) || b.severity - a.severity);
    const outside = groups.markedly_above.length + groups.markedly_below.length + groups.above_typical.length + groups.below_typical.length;
    const cohortIssue = ["cohort_unavailable", "target_unavailable", "stale_target"].includes(sample.cohort_state);
    const cohortContext = cohortIssue ? `cohort ${sample.cohort_state.replace(/_/g, " ")}`
      : cohortReadiness(sample, metrics);
    const out = [
      h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Findings`),
      h("div", { class: "findings-head" }, h("h1", { class: "view-title" }, "Findings"), flagPill("CLIA")),
      h("p", { class: "findings-summary" }, `${outside} outside a reference · ${groups.not_interpretable.length} not interpretable · ${groups.no_reference.length} without a reference (${cohortContext}) · ${groups.in_range.length} in range`),
    ];
    if (cohortIssue) out.push(h("p", { class: "problem" }, `Cohort reference ${sample.cohort_state.replace(/_/g, " ")}. Cohort states are unavailable for this view; measurements and published references remain visible. ${sample.cohort_problem ? sample.cohort_problem.fix : "Check cohort build and reload."}`));
    if (sample.malformed_result_selections) out.push(h("article", { class: "finding-card problem" },
      h("h2", {}, `Malformed result selection (sample ${sample.id12})`)));
    if (sample.cohort_state === "ready" && sample.cohort_problem) out.push(h("p", { class: "problem" }, `Panel of normals unavailable: copy-number and dependent cohort states are withheld; independent metrics remain comparable. ${sample.cohort_problem.fix}`));
    for (const id of sample.unreadable_results || []) {
      out.push(h("article", { class: "finding-card problem" },
        h("h2", {}, `${id}: result unreadable (sample ${sample.id12})`)));
    }
    for (const id of sample.ambiguous_results || []) {
      out.push(h("article", { class: "finding-card problem" },
        h("h2", {}, `${id}: result selection ambiguous (sample ${sample.id12})`)));
    }
    for (const a of sample.algorithms.filter((x) => x.status === "failed")) {
      out.push(h("div", { class: "problem" }, `${a.algorithm_id} failed: ${a.failure ? a.failure.first_line : ""}. `, h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Details in Trust/QC →")));
    }
    const seen = new Set();
    let first = true;
    if (!outside) {
      const withReference = metrics.filter((m) => m.state !== "no_reference" && m.state !== "not_interpretable").length;
      const withoutReference = groups.no_reference.length;
      out.push(h("p", { class: "empty" }, `No metric is outside a reference. ${withReference} ${withReference === 1 ? "metric has" : "metrics have"} a reference; ${withoutReference} ${withoutReference === 1 ? "metric has" : "metrics have"} no reference (${cohortContext}). `,
        h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Trust/QC"), " · ", h("a", { href: `#/sample/${sample.id12}/stability` }, "Stability")));
    }
    const titles = { markedly_above: "Markedly above", markedly_below: "Markedly below", above_typical: "Above typical", below_typical: "Below typical" };
    for (const key of Object.keys(titles)) {
      if (!groups[key].length) continue;
      out.push(h("h2", {}, `${titles[key]} (${groups[key].length})`));
      for (const m of groups[key]) { out.push(card(sample, m, seen, first)); first = false; }
    }
    const reasons = (list) => { const c = {}; for (const m of list) c[m.reason || "—"] = (c[m.reason || "—"] || 0) + 1; return Object.entries(c).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · "); };
    const extras = (m) => [h("span", {}, ...(m.entry && m.entry.flags || []).map((flag) => metricFlag(m, flag)),
      ...(m.entry && m.entry.license_ids || []).map((id) => seen.has(id) ? licenseRef(sample, id) : (seen.add(id), licenseRef(sample, id)))), unverifiedBadge(m.entry)];
    out.push(collapsed(`Not interpretable (${groups.not_interpretable.length})`, reasons(groups.not_interpretable), ["Metric", "Value", "Reason", "Needs", ""],
      groups.not_interpretable.map((m) => [m.label, fmtMetric(m), (m.reason || "").replace(/_/g, " "), m.entry && m.entry.min_depth_x ? `${m.entry.min_depth_x}x` : "—", extras(m)])));
    out.push(collapsed(`No reference (${groups.no_reference.length})`, reasons(groups.no_reference), ["Metric", "Value", "Reason", "Reference shown", ""],
      groups.no_reference.map((m) => [m.label, fmtMetric(m), (m.reason || "").replace(/_/g, " "), m.caption || "—", extras(m)])));
    out.push(collapsed(`In range (${groups.in_range.length})`, "", ["Metric", "Value", "Reference", ""],
      groups.in_range.map((m) => [m.label, fmtMetric(m), strip(m), extras(m)])));
    // Full callouts for licenses first used in the grouped rows (callout-once per route).
    const pending = (sample.licenses || []).filter((lic) => !document.getElementById(`license-${lic.license_id}`) && [...seen].includes(lic.license_id));
    const cardIds = new Set(out.flatMap((node) => node instanceof Element ? [...node.querySelectorAll(".license-callout")].map((c) => c.id) : []));
    const remaining = pending.filter((lic) => !cardIds.has(`license-${lic.license_id}`));
    if (remaining.length) out.push(h("section", { class: "panel" }, h("h2", {}, "License callouts"), ...remaining.map((lic) => h("div", { class: "license-callout", id: `license-${lic.license_id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)))));
    out.push(composition(sample));
    const expand = h("button", { type: "button", class: "control" }, "Expand all");
    expand.addEventListener("click", () => { for (const b of document.querySelectorAll(".finding-card button[aria-expanded]")) { b.setAttribute("aria-expanded", "true"); document.getElementById(b.getAttribute("aria-controls")).hidden = false; } });
    out.splice(3, 0, expand);
    return out;
  }

  // Cell-type composition (cell-origin): dot and interval per cell type, no donut.
  function composition(sample) {
    const section = h("section", { class: "panel", "aria-labelledby": "composition-title" },
      h("h2", { id: "composition-title" }, "Cell-type composition (cell-origin)"),
      flagPill("LEGAL", "FLAG: LEGAL — research-only licenses; atlas terms unknown."));
    const a = sample.algorithms.find((x) => x.algorithm_id === "cell-origin");
    if (!a || a.status !== "ok") {
      section.append(!a && unreadableArtifact(sample, "cell-origin")
        ? absent(a, "cell-origin", sample)
        : h("p", { class: "empty" }, a ? `cell-origin ${a.status}${a.skip_reason ? ` (${a.skip_reason})` : ""}: no composition at this depth.` : "cell-origin has no result for this sample."));
      return section;
    }
    const slot = h("div", {}, h("p", {}, "Loading…"));
    section.append(slot);
    fetch(`/api/samples/${sample.id12}/artifact/full/cell-origin/composition`, { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error("data reference invalid"); return r.json(); })
      .then((data) => {
        const rows = data.contributors.filter((c) => c.fraction > 0 || (c.interval && c.interval[1] > 0)).sort((x, y) => y.fraction - x.fraction);
        const w = 560, rowH = 22, left = 170, hgt = rows.length * rowH + 30;
        const max = Math.max(0.05, ...rows.map((c) => (c.interval ? c.interval[1] : c.fraction)));
        const x = (v) => left + (v / max) * (w - left - 20);
        const label = `Cell-type shares with ${Math.round(data.confidence_level * 100)}% bootstrap intervals; ${rows.length} cell types above zero.`;
        const marks = rows.flatMap((c, i) => {
          const y = 20 + i * rowH;
          const out = [svg("text", { x: left - 6, y: y + 4, "text-anchor": "end", class: "axis" }, c.contributor_id)];
          if (c.interval) out.push(svg("line", { x1: x(c.interval[0]), x2: x(c.interval[1]), y1: y, y2: y, class: "ci" }));
          out.push(svg("text", { x: x(c.fraction), y: y + 5, "text-anchor": "middle", class: "pt" }, "●"));
          return out;
        });
        const chartSvg = svg("svg", { viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label, class: "composition-svg" }, svg("title", {}, label), ...marks);
        const compositionRow = (c) => [c.contributor_id,
          rawCell(fmt(c.fraction, "fraction"), c.fraction, "fraction"),
          rawCell(fmt(c.interval ? c.interval[0] : null, "fraction"), c.interval ? c.interval[0] : null, "fraction"),
          rawCell(fmt(c.interval ? c.interval[1] : null, "fraction"), c.interval ? c.interval[1] : null, "fraction"),
          c.interval_state];
        slot.replaceWith(h("div", {}, chartSvg,
          dataTable(`Plotted cell-type composition (${Math.round(data.confidence_level * 100)}% bootstrap interval)`, ["Cell type", "Share", "Interval low", "Interval high", "Interval"],
            rows.map(compositionRow)),
          dataTable("All cell-type contributions, including zero-share rows", ["Cell type", "Share", "Interval low", "Interval high", "Interval"],
            data.contributors.map(compositionRow))));
      })
      .catch((error) => slot.replaceWith(h("div", { class: "problem" }, `Chart could not be drawn (${error.message}).`)));
    return section;
  }

  function collapsed(title, breakdown, headers, rows) {
    const cell = (c) => h("td", {}, ...(Array.isArray(c) ? c : [c instanceof Node ? c : String(c)]));
    return h("details", { class: "finding-group" }, h("summary", {}, title, breakdown ? h("span", { class: "help" }, `  ${breakdown}`) : ""),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map(cell)))))));
  }

  // -- Stability ------------------------------------------------------------------------------
  function stability(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Stability`), h("h1", { class: "view-title" }, "Stability")];
    for (const issue of (sample.stability_unavailable || []))
      out.push(h("p", { class: "problem" }, `${issue.algorithm_id}: ${issue.reason}; no metric-level Stability panel can be shown.`));
    if (!sample.curve.downsampled) out.push(h("p", { class: "empty" }, `This run used --no-downsample. Run traceback lab run ${sample.id12} to draw the curve.`));
    if (!sample.repeatability) {
      const recorded = (sample.stability || []).some((panel) =>
        [panel.split_status?.A?.state, panel.split_status?.B?.state].some((state) => state && state !== "not_recorded"));
      out.push(h("p", { class: "help" }, recorded
        ? "Split halves were recorded, but no comparable pair is available."
        : `Split-half markers absent: run traceback lab run ${sample.id12}.`));
    }
    const grid = h("div", { class: "multiples" });
    const tableRows = [];
    for (const panel of sample.stability) {
      const w = 240, hgt = 140, pad = 28;
      const fracs = [0.1, 0.25, 0.5, 1.0];
      const x = (f) => pad + ((Math.log10(f) - Math.log10(0.08)) / (Math.log10(1.2) - Math.log10(0.08))) * (w - pad - 8);
      const unit = (panel.points.find((p) => p.unit && p.unit !== "none") || {}).unit ||
        [panel.split_status?.A?.unit, panel.split_status?.B?.unit].find((value) => value && value !== "none") || "none";
      const clip = 50;
      const splitDevs = [panel.split_status?.A?.deviation, panel.split_status?.B?.deviation].filter(Number.isFinite).map(Math.abs);
      const maxAbs = unit === "unit" ? Math.max(1e-9, ...splitDevs, ...panel.points.map((p) => Math.abs(p.deviation || 0)), ...(panel.points.map((p) => p.band ? Math.max(Math.abs(p.band.lo), Math.abs(p.band.hi)) : 0).filter(Number.isFinite))) : clip;
      const y = (v) => { const c = Math.max(-maxAbs, Math.min(maxAbs, v)); return hgt / 2 - (c / maxAbs) * (hgt / 2 - 12); };
      const marks = [svg("line", { x1: pad, x2: w - 8, y1: y(0), y2: y(0), class: "zero" })];
      for (const p of panel.points) {
        if (p.band && Number.isFinite(p.band.lo) && Number.isFinite(p.band.hi)) marks.push(svg("rect", { x: x(p.fraction) - 6, width: 12, y: y(p.band.hi), height: Math.max(1, y(p.band.lo) - y(p.band.hi)), class: "band" }));
        if ((p.state === "ok_usable" || p.state === "ok_unstable") && Number.isFinite(p.deviation)) {
          const clipped = unit !== "unit" && Math.abs(p.deviation) > clip;
          marks.push(svg("text", { x: x(p.fraction), y: y(p.deviation) + 4, "text-anchor": "middle", class: "pt" }, clipped ? (p.deviation > 0 ? "↑" : "↓") : "●"));
        } else {
          const description = (CELL[p.state] || ["", p.state || "gap"])[1];
          const gapLabel = p.reason === "result unreadable" ? "unreadable"
            : p.reason === "result selection ambiguous" ? "ambiguous"
              : p.state === "ok_unvalidated" ? "no verdict" : description.slice(0, 10);
          marks.push(svg("text", { x: x(p.fraction), y: hgt - 6, "text-anchor": "middle", class: "gap" }, gapLabel));
        }
      }
      if (unit !== "none") {
        for (const [dx, d] of [[-9, panel.split_status?.A?.deviation], [9, panel.split_status?.B?.deviation]]) {
          if (!Number.isFinite(d)) continue;
          const clipped = unit !== "unit" && Math.abs(d) > clip;
          marks.push(svg("text", { x: x(0.5) + dx, y: y(d) + 4, "text-anchor": "middle", class: "split" }, clipped ? (d > 0 ? "⇡" : "⇣") : "○"));
        }
      }
      const metric = sample.algorithms.find((algorithm) => algorithm.algorithm_id === panel.algorithm_id)
        ?.metrics.find((entry) => entry.metric_id === panel.metric_id);
      const metricValue = (value, options = {}) => metric
        ? fmtMetricValue(metric, value, { algorithmId: panel.algorithm_id, ...options })
        : fmt(value, null, options);
      const deviationValue = (value) => unit === "percent" ? fmt(value, "%")
        : unit === "unit" ? metricValue(value, { role: "difference" }) : "unavailable";
      const measuredCell = (value) => rawCell(metricValue(value), value, metric?.unit);
      const deviationCell = (value) => rawCell(deviationValue(value), value,
        unit === "percent" ? "%" : unit === "unit" ? metric?.unit : "scale not validated");
      const halfStatus = (half) => {
        const part = panel.split_status && panel.split_status[half];
        if (!part) return "status unavailable";
        if (part.state === "ok") return `ok (${metricValue(part.value)})`;
        return `${part.state.replace(/_/g, " ")}${part.reason ? ` (${part.reason.replace(/_/g, " ")})` : ""}`;
      };
      const splitSummary = `split A ${halfStatus("A")}; split B ${halfStatus("B")}` +
        (panel.split_warning ? `; ${panel.split_warning}` : "");
      const hasSplitHalf = Boolean(panel.split_status) &&
        (panel.split_status.A?.state !== "not_recorded" || panel.split_status.B?.state !== "not_recorded");
      const splitVerdict = panel.split?.stable === true ? "stable" : panel.split?.stable === false ? "unstable" : "no split-half stability verdict";
      const absoluteDiff = panel.split ? panel.split.absolute_diff ?? Math.abs(panel.split.a - panel.split.b) : null;
      const splitDiffText = Number.isFinite(absoluteDiff)
        ? `|A−B| = ${metricValue(absoluteDiff, { role: "difference" })}; relative difference ${fmt(panel.split.relative_abs_diff * 100, "%")}`
        : "";
      for (const f of fracs) marks.push(svg("text", { x: x(f), y: hgt - 18, "text-anchor": "middle", class: "axis" }, `${f * 100}%`));
      const scaleLabel = unit === "none" ? "no validated stability deviation scale" : unit === "unit" ? "the metric's unit" : "%";
      const label = `${panel.label}: ${panel.verdict}. Deviation from full depth: ${scaleLabel}${hasSplitHalf ? `; ${splitSummary}; ${splitVerdict}` : ""}.`;
      const pointRows = panel.points.map((p) => ["Measured fraction", p.fraction * 100, measuredCell(p.value), deviationCell(p.deviation),
        p.band && Number.isFinite(p.band.lo) && Number.isFinite(p.band.hi) ? deviationCell(p.band.lo) : "unavailable",
        p.band && Number.isFinite(p.band.lo) && Number.isFinite(p.band.hi) ? deviationCell(p.band.hi) : "unavailable",
        stateReason(p.state, p.reason, p.state || "—")]);
      if (unit !== "none") for (const half of ["A", "B"]) {
        const part = panel.split_status?.[half];
        if (Number.isFinite(part?.deviation))
          pointRows.push([`Split ${half}`, 50, measuredCell(part.value), deviationCell(part.deviation), "unavailable", "unavailable",
            stateReason(part.state, part.reason, part.state || "—")]);
      }
      grid.append(h("figure", { class: "multiple" }, h("figcaption", {}, h("strong", {}, panel.label), ` — ${panel.verdict}`),
        svg("svg", { viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label, class: "multiple-svg" }, svg("title", {}, label), ...marks),
        dataTable(`${panel.label}: plotted stability points`, ["Mark", "Input fraction (%)", "Value",
          unit === "percent" ? "Deviation (%)" : "Deviation", unit === "percent" ? "Band low (%)" : "Band low",
          unit === "percent" ? "Band high (%)" : "Band high", "State"], pointRows),
        h("p", { class: "help" }, unit === "none" ? "No validated stability deviation scale; measured values remain in the table." : unit === "unit" ? "y: difference from full depth (metric's unit)" : "y: relative deviation from full depth, % (clipped ±50%)"),
        hasSplitHalf ? h("p", { class: "help" }, `Split halves: ${splitSummary}. `,
          splitDiffText ? `${splitDiffText}. ` : "",
          splitVerdict === "no split-half stability verdict" ? "No split-half stability verdict." : `Split-half verdict: ${splitVerdict}.`) : ""));
      for (const p of panel.points) tableRows.push([panel.label, `${p.fraction * 100}%`, measuredCell(p.value), deviationCell(p.deviation),
        stateReason(p.state, p.reason, p.state || "—")]);
      if (panel.split) tableRows.push([panel.label, "split A / B",
        rawCell(`${metricValue(panel.split.a)} / ${metricValue(panel.split.b)}`,
          `${panel.split.a} / ${panel.split.b}`, metric?.unit),
        rawCell(splitDiffText, absoluteDiff, metric?.unit),
        panel.split_warning ? `${splitVerdict}; ${panel.split_warning}` : splitVerdict]);
      else if (hasSplitHalf) {
        const halfDeviation = (half) => {
          const value = panel.split_status?.[half]?.deviation;
          return Number.isFinite(value) ? deviationValue(value) : "unavailable";
        };
        tableRows.push([panel.label, "split A / B",
          rawCell(`${metricValue(panel.split_status?.A?.value)} / ${metricValue(panel.split_status?.B?.value)}`,
            `${panel.split_status?.A?.value ?? "null"} / ${panel.split_status?.B?.value ?? "null"}`, metric?.unit),
          `${halfDeviation("A")} / ${halfDeviation("B")} (each vs full)`, splitSummary]);
      }
    }
    out.push(grid, dataTable("Stability: every point", ["Metric", "Fraction", "Value", "Deviation", "State"], tableRows));
    const shown = new Set(sample.stability.map((p) => p.algorithm_id));
    const ids = new Set();
    for (const a of sample.algorithms) if (shown.has(a.algorithm_id)) for (const m of a.metrics) for (const id of (m.entry && m.entry.license_ids) || []) ids.add(id);
    const lics = (sample.licenses || []).filter((lic) => ids.has(lic.license_id));
    if (lics.length) out.push(h("section", { class: "panel" }, h("h2", {}, "License callouts"), ...lics.map((lic) => h("div", { class: "license-callout", id: `license-${lic.license_id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)))));
    return out;
  }

  async function clia() {
    const c = await cliaCopy();
    return [h("p", { class: "back" }, h("a", { href: "#/" }, "← Back to Lab home")), h("h1", { class: "view-title" }, c.title), ...paragraphs(c.full), h("p", { class: "help" }, `Last reviewed: ${c.last_reviewed}`)];
  }

  // -- Evidence -------------------------------------------------------------------------------
  function evidence(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Evidence`), h("h1", { class: "view-title" }, "Evidence")];
    out.push(h("p", { class: "help" }, "Tier is judged as run here (ONT, this depth); the original evidence is shown beside it. Every algorithm the BAM can support is listed, weak ones included, with its weakness in the footnote."));
    // One full block per license at the top; pills below (callout-once).
    const used = sample.licenses || [];
    if (used.length) out.push(h("section", { class: "panel" }, h("h2", {}, "License callouts"),
      ...used.map((lic) => h("div", { class: "license-callout", id: `license-${lic.license_id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)))));
    const columns = h("div", { class: "evidence-columns" });
    for (const tier of ["PROVEN", "NEWER", "SPECULATIVE"]) {
      const rows = sample.catalog.filter((c) => c.tier === tier);
      columns.append(h("section", { class: "panel evidence-column", "aria-labelledby": `ev-${tier}` },
        h("h2", { id: `ev-${tier}` }, tierBadge(tier), ` ${rows.length}`),
        ...rows.map((c) => h("article", { class: "evidence-card" },
          h("h3", {}, c.label), h("p", { class: "help" }, c.algorithm_id),
          h("p", {}, `Original evidence: ${c.origin_tier || "—"} (${c.origin_platform || "—"})`),
          h("p", {}, "Footnote: ", c.footnote || "none published", c.footnote_source ? ` (${c.footnote_source})` : "",
            spacedUnverifiedBadge(c.has_library_entry ? c : null)),
          h("p", {}, c.status === "skipped" ? `skipped (${(c.skip_reason || "").replace(/_/g, " ")})` : c.status === "ok" ? "ran" : c.status,
            c.min_depth_x ? ` · literature minimum ${c.min_depth_x}x` : ""),
          h("p", {}, ...c.license_ids.map((id) => licenseRef(sample, id)))))));
    }
    out.push(columns);
    out.push(h("section", { class: "panel" }, h("h2", {}, "Excluded (not computable from this BAM)"),
      h("ul", {}, ...sample.excluded.map((x) => h("li", {}, h("strong", {}, x.id), `: ${x.reason}`)))));
    return out;
  }

  // -- Methods --------------------------------------------------------------------------------
  function methods(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Methods`), h("h1", { class: "view-title" }, "Methods"),
      h("p", {}, `Interpretation library v${sample.interpretation_version} · cohort ${sample.cohort_version || "none yet"} · provenance stamp only (no signing). `, flagPill("PII", "FLAG: PII — file paths often embed donor names. Hashes only, never paths."))];
    const rows = [];
    const seen = new Set();
    const firstUse = (id) => {
      if (seen.has(id)) return licenseRef(sample, id);
      seen.add(id);
      const lic = (sample.licenses || []).find((x) => x.license_id === id);
      return lic ? h("div", { class: "license-callout", id: `license-${id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)) : "";
    };
    for (const a of sample.algorithms) {
      if (a.hidden) continue;
      for (const m of a.metrics) {
        if (m.value !== null && typeof m.value === "object") continue;
        const e = m.entry || {};
        const ref = e.reference || {};
        rows.push(h("tr", {},
          h("th", { scope: "row" }, m.label), h("td", {}, rawCell(fmtMetric(m, { algorithmId: a.algorithm_id }), m.value, m.unit)),
          h("td", {}, a.provenance.tool, " ", a.provenance.tool_version),
          h("td", { class: "mono" }, h("details", {}, h("summary", {}, a.provenance.parameters_sha256.slice(0, 12)), provenance(sample, a.algorithm_id))),
          h("td", { class: "mono" }, a.provenance.input_id.slice(0, 12)),
          h("td", {}, a.provenance.reference_id),
          h("td", {}, a.provenance.timestamp_utc),
          h("td", {}, ...(e.license_ids || []).map(firstUse)),
          h("td", {}, (e.citations || []).join(", ") || "—", spacedUnverifiedBadge(m.entry)),
          h("td", {}, ref.kind === "none" || !ref.kind ? "none" : `${ref.kind}${ref.source ? ": " + ref.source : ""}`)));
      }
    }
    out.push(h("div", { class: "table-wrap", tabindex: "0", role: "region",
      "aria-label": "Every number with its provenance" }, h("table", { class: "data-table" },
      h("caption", {}, "Every number with its provenance"),
      h("thead", {}, h("tr", {}, ...["Metric", "Value", "Tool", "Provenance stamp (parameters sha256)", "Input", "Reference", "Computed (UTC)", "License", "Citation", "Range source"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ...rows))));
    return out;
  }

  // -- shared helpers for L1.7 ------------------------------------------------------------------
  function artifact(sample, algorithmId, panelId, label = "full") {
    return fetch(`/api/samples/${sample.id12}/artifact/${label}/${algorithmId}/${panelId}`, { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error("data reference invalid"); return r.json(); });
  }
  function algo(sample, id) { return sample.algorithms.find((a) => a.algorithm_id === id); }
  function value(sample, id, metric) {
    const a = algo(sample, id);
    const m = a && a.status === "ok" ? a.metrics.find((x) => x.metric_id === metric) : null;
    return m ? m.value : null;
  }
  // Tier, performance footnote, full license callouts (first use on this route) and provenance.
  function context(sample, algorithmId, seen) {
    const c = (sample.catalog || []).find((x) => x.algorithm_id === algorithmId);
    const out = [];
    if (c) out.push(h("p", { class: "help" }, tierBadge(c.tier), ` As run here (${sample.platform === "illumina" ? "Illumina" : sample.platform === "ont" ? "ONT" : "unverified platform"}). Original evidence: ${c.origin_tier || "—"}. Footnote: ${c.footnote || "none published"}`,
      spacedUnverifiedBadge(c.has_library_entry ? c : null)));
    for (const id of (c ? c.license_ids : [])) {
      if (seen.has(id)) { out.push(licenseRef(sample, id)); continue; }
      seen.add(id);
      const lic = (sample.licenses || []).find((x) => x.license_id === id);
      if (lic) out.push(h("div", { class: "license-callout", id: `license-${id}` }, h("h3", {}, lic.headline), h("p", {}, lic.body)));
    }
    out.push(h("details", {}, h("summary", {}, "Provenance"), provenance(sample, algorithmId)));
    return out;
  }

  function unreadableArtifact(sample, id) {
    return (sample.unreadable_results || []).includes(id);
  }
  function absent(a, what, sample) {
    if (!a && sample && unreadableArtifact(sample, what)) {
      return h("p", { class: "problem" }, "Chart could not be drawn (selected result unavailable).");
    }
    return h("p", { class: "empty" }, !a ? `${what}: not computed for this sample.` : `${what}: ${a.status}${a.skip_reason ? ` (${a.skip_reason.replace(/_/g, " ")})` : ""}.`);
  }

  // -- Signals: a measured starting path, followed by the full signal families -----------------
  const GUIDE_SIGNALS = [
    {
      algorithm: "fragment-size", metric: "mode_bp", title: "Fragment length",
      definition: "The most frequent aligned reference span (reference end minus reference start) from 100–250 bp.",
      denominator: "100–250 bp spans on primary mapped canonical-contig reads, MAPQ ≥20, excluding duplicates and QC failures",
      limitation: "An aligned span is not a directly observed physical fragment length. Library size bias and sample handling can shift the mode; it does not identify a tissue or disease.",
    },
    {
      algorithm: "global-5mc", metric: "cpg_5mc_fraction", title: "Genome-wide 5mC fraction",
      definition: "CpG C+m site calls assigned to 5mC by the largest of the 5mC, 5hmC, and canonical probabilities, divided by all eligible CpG C+m site calls, both strands.",
      denominator: "CpG C+m site calls on primary mapped canonical-contig reads, MAPQ ≥20, excluding duplicates and QC failures",
      limitation: "This is exploratory and depends on the basecaller modification model, call quality, and cell mixture. No validated reference is supplied for this run; this value alone supports no biological conclusion.",
    },
    {
      algorithm: "bam-qc", metric: "mapped_fraction", title: "Mapped-read share",
      definition: "Mapped primary read rows divided by all primary read rows in the read summary.",
      denominator: "Primary read rows",
      limitation: "This describes alignment and input quality, not the origin of cfDNA. Unmapped reads can reflect adapters, short inserts, contamination, or a reference mismatch.",
    },
  ];

  function guidedSignals(sample) {
    const cards = GUIDE_SIGNALS.map((guide) => {
      const algorithm = algo(sample, guide.algorithm);
      const metric = algorithm?.status === "ok" ? algorithm.metrics.find((m) => m.metric_id === guide.metric) : null;
      const available = metric && typeof metric.value === "number" && Number.isFinite(metric.value);
      const unreadable = unreadableArtifact(sample, guide.algorithm);
      const ambiguous = (sample.ambiguous_results || []).includes(guide.algorithm);
      const reason = available ? null : unreadable ? "selected result unreadable"
        : ambiguous ? "result selection ambiguous"
          : !algorithm ? "method not computed for this sample"
            : algorithm.status !== "ok" ? `${algorithm.status}${algorithm.skip_reason ? ` (${algorithm.skip_reason.replace(/_/g, " ")})` : ""}`
              : metric ? (metric.reason || "measurement unavailable").replace(/_/g, " ") : "metric not recorded";
      const stabilityPanel = (sample.stability || []).find((panel) =>
        panel.algorithm_id === guide.algorithm && panel.metric_id === guide.metric);
      const denominator = available && Number.isSafeInteger(metric.n)
        ? `${guide.denominator}: ${fmtCount(metric.n)}` : `${guide.denominator}: count unavailable`;
      const qc = available && metric.qc_band
        ? `QC band: ${metric.qc_band} (provisional team default).` : "QC band: none recorded for this metric.";
      const stabilityText = stabilityPanel ? `Stability: ${stabilityPanel.verdict}.` : "Stability: no metric-level panel recorded.";
      const card = h("article", { class: `signal-guide-card${available ? "" : " signal-guide-unavailable"}`,
        "data-guide-signal": `${guide.algorithm}/${guide.metric}` },
        h("h3", {}, guide.title),
        h("p", { class: "signal-guide-value" }, "Observed: ", h("strong", {}, available
          ? fmtMetric(metric, { algorithmId: guide.algorithm }) : `unavailable (${reason})`)),
        h("p", {}, h("strong", {}, "Measurement: "), guide.definition),
        h("p", {}, h("strong", {}, "Denominator / filter: "), denominator),
        h("p", { class: "help" }, qc, " ", stabilityText, " ",
          h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Trust/QC"), " · ",
          h("a", { href: `#/sample/${sample.id12}/stability` }, "Stability")),
        h("p", { class: "help" }, h("strong", {}, "Interpretation limit: "), guide.limitation),
        h("p", { class: "help" }, `Source result: ${guide.algorithm}/${guide.metric}. `,
          h("a", { href: `#/sample/${sample.id12}/methods` }, "Method and source →")));
      if (algorithm) card.append(h("details", {}, h("summary", {}, "Result provenance"), provenance(sample, guide.algorithm)));
      return { card, available };
    });
    const available = cards.filter((row) => row.available).map((row) => row.card);
    const unavailable = cards.filter((row) => !row.available).map((row) => row.card);
    return h("section", { class: "panel signal-guide", "aria-labelledby": "signal-guide-title" },
      h("h2", { id: "signal-guide-title" }, "Start with three measured signals"),
      h("p", { class: "help" }, sample.example
        ? "This is a seeded synthetic example. Its planted fragment-length and modification-call inputs illustrate the workflow; observed values below are computed from this run and imply no biological finding."
        : "These values come from this sample's recorded results. Read each measurement with its denominator, quality, and stability."),
      h("div", { class: "signal-guide-grid" }, ...available),
      unavailable.length ? h("div", { class: "signal-guide-missing" },
        h("h3", {}, `Unavailable guided signals (${unavailable.length})`),
        h("div", { class: "signal-guide-grid" }, ...unavailable)) : "");
  }

  function signals(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Signals`), h("h1", { class: "view-title" }, "Signals"), guidedSignals(sample)];
    const families = [
      ["fragmentomics", "Fragmentomics", fragmentSignals],
      ["copy-number", "Copy number", copyNumberSignals],
      ["methylation", "Methylation", methylationSignals],
    ];
    const tabs = h("div", { class: "subtabs", role: "group", "aria-label": "Signal families" });
    const body = h("div", {});
    const show = (index) => {
      for (const [i, button] of [...tabs.children].entries()) button.setAttribute("aria-pressed", String(i === index));
      body.replaceChildren(...families[index][2](sample));
    };
    families.forEach(([, label], i) => tabs.append(h("button", { type: "button", class: "control subtab", "data-requires-server": "", "aria-pressed": "false", onclick: () => show(i) }, label)));
    out.push(tabs, body);
    const initial = ["fragment-size", "end-motifs", "delfi-ratio", "finaletoolkit-xcheck", "tss-profile", "long-tail", "mtdna", "breakpoint-motifs", "griffin", "aligned-end-structure", "lionheart"].some((id) => algo(sample, id) || (id !== "long-tail" && id !== "mtdna" && unreadableArtifact(sample, id)))
      ? 0 : algo(sample, "copy-number") || unreadableArtifact(sample, "copy-number") ? 1 : 2;
    show(initial);
    return out;
  }

  function breakpointMotifSignals(sample, seen) {
    const a = algo(sample, "breakpoint-motifs");
    if (!a && !unreadableArtifact(sample, "breakpoint-motifs")) return [];
    const section = h("section", { class: "panel" }, h("h2", {}, "CpG-proximal aligned ends"), flagPill("LEGAL"));
    if (!a || a.status !== "ok") return [section, absent(a, "breakpoint-motifs", sample)];
    section.append(
      h("p", {}, "These are aligned-reference-span start and end−1 contexts; physical cleavage sites are not established by this BAM. Near and far 4-mer counts exclude invalid or off-contig contexts."),
      ...context(sample, "breakpoint-motifs", seen),
    );
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading 4-mer counts…"));
    section.append(slot);
    artifact(sample, "breakpoint-motifs", "cpg-end-motifs").then((data) => {
      if (!Array.isArray(data.motif) || data.motif.length !== 256 ||
          !Array.isArray(data.near_count) || data.near_count.length !== 256 ||
          !Array.isArray(data.far_count) || data.far_count.length !== 256) {
        throw new Error("motif panel dimensions invalid");
      }
      const rows = data.motif.map((motif, i) => [motif, data.near_count[i], data.far_count[i]]);
      const table = h("table", { class: "data-table" },
        h("caption", {}, "Aligned-end 4-mers near and far from CpG; 256 motifs"),
        h("thead", {}, h("tr", {}, ...["4-mer", "Near count", "Far count"].map((name) => h("th", { scope: "col" }, name)))),
        h("tbody", {}, ...rows.map(([motif, near, far]) =>
          h("tr", {}, h("th", { scope: "row" }, motif), h("td", { class: "num" }, fmtCount(near)), h("td", { class: "num" }, fmtCount(far))))));
      const details = h("details", {}, h("summary", {}, "All 256 aligned-end motifs"), h("div", { class: "table-wrap" }, table));
      slot.replaceWith(
        h("p", {}, `Near ends: ${fmtCount(data.near_ends)}; far ends: ${fmtCount(data.far_ends)}. Invalid contexts: near ${fmtCount(data.invalid_context_near)}, far ${fmtCount(data.invalid_context_far)}.`),
        data.methylation_link && data.methylation_link.status === "unavailable"
          ? h("p", { class: "help" }, `Methylation link unavailable: ${data.methylation_link.reason || "no supported joint input"}.`) : "",
        details,
      );
    }).catch((error) => slot.replaceWith(h("p", { class: "problem" }, `4-mer table unavailable (${error.message}). The scalar result remains in Findings.`)));
    return [section];
  }

  function alignedEndSignals(sample, seen) {
    const a = algo(sample, "aligned-end-structure");
    if (!a && !unreadableArtifact(sample, "aligned-end-structure")) return [];
    const section = h("section", { class: "panel" }, h("h2", {}, "Open-chromatin aligned ends"), flagPill("LEGAL"));
    if (!a || a.status !== "ok") return [section, absent(a, "aligned-end-structure", sample)];
    section.append(h("p", { class: "help" },
      "Exploratory ONT aligned-span proxies, not physical cleavage validation, tissue-origin inference, or a clinical cutoff. The catalogues are pinned to hg38; published OCF and preferred-end studies used paired-end short reads."));
    const slot = h("div", {}, h("p", {}, "Loading pinned end counts…"));
    section.append(...context(sample, "aligned-end-structure", seen), slot);
    artifact(sample, "aligned-end-structure", "aligned-end-structure").then((data) => {
      const rows = data.offset_bp.map((bp, i) => [bp, data.u_count[i], data.d_count[i]]);
      const spans = data.aligned_span_length_bins_bp;
      const spanRows = spans ? [
        ["Under 100", spans.lt100], ["100–200", spans["100_200"]], ["201–500", spans["201_500"]],
        ["501–2000", spans["501_2000"]], ["Over 2000", spans.gt2000],
      ] : null;
      slot.replaceWith(h("div", {},
        h("p", { class: "help" }, `Pinned catalogue: ${data.source_label}; centers: ${data.centers_source}; end sites: ${data.preferred_source}; terms: ${data.terms}. These operator labels identify the declared source, not independent assembly or tissue validation.`),
        h("p", {}, `Observed eligible depth: ${fmt(data.observed_depth_x, "x")}; valid molecules: ${fmtCount(data.valid_molecules)}; eligible centers: ${fmtCount(data.eligible_centers)} of ${fmtCount(data.catalogue_centers)}; eligible pinned sites: ${fmtCount(data.eligible_preferred_sites)} of ${fmtCount(data.catalogue_preferred_sites)}.`),
        h("p", {}, `OCF-like left/right contributions: ${fmt(data.ocf_like_left, "end-count difference", { unitDisplay: false })} / ${fmt(data.ocf_like_right, "end-count difference", { unitDisplay: false })} end-count differences from ${fmtCount(data.ocf_like_left_ends)} / ${fmtCount(data.ocf_like_right_ends)} ends in the two measurement windows. The score is unavailable if either window has no ends. Exact preferred-site U/D hits: ${fmtCount(data.preferred_u_hits)} / ${fmtCount(data.preferred_d_hits)} of ${fmtCount(data.valid_ends)} valid ends.`),
        spanRows ? dataTable("Aligned spans by length (bp)", ["Aligned span (bp)", "Molecules"], spanRows)
          : h("p", { class: "help" }, "Aligned-span length diagnostic was not recorded."),
        h("p", { class: "help" }, `Left D peak offset: ${fmt(data.left_d_peak_offset_bp, "bp")}; right U peak offset: ${fmt(data.right_u_peak_offset_bp, "bp")}. These observed endpoint peaks do not measure soft clipping or validate physical cleavage.`),
        chart("Upstream and downstream aligned-end counts around pinned hg38 centers.", {
          grid: { left: 55, right: 20, top: 20, bottom: 40 },
          xAxis: { type: "value", name: "bp from center", min: -100, max: 100 },
          yAxis: { type: "value", name: "raw ends (not depth-normalized)" },
          series: [
            { type: "line", name: "U", showSymbol: false, data: rows.map((row) => [row[0], row[1]]) },
            { type: "line", name: "D", showSymbol: false, data: rows.map((row) => [row[0], row[2]]) },
          ],
        }, dataTable("Aligned-end counts by offset", ["Offset (bp)", "U ends", "D ends"], rows))));
    }).catch(() => slot.replaceWith(h("p", { class: "problem" }, "Pinned end-count panel could not be loaded.")));
    return [section];
  }

  function lionheartSignals(sample, seen) {
    const a = algo(sample, "lionheart");
    if (!a && !unreadableArtifact(sample, "lionheart")) return [];
    const section = h("section", { class: "panel" }, h("h2", {}, "LIONHEART cell-type scores"), flagPill("LEGAL"),
      h("p", {}, "Experimental ONT single-end extraction. Scores are sample-standardized correlations, not cell fractions or cancer predictions."),
      ...context(sample, "lionheart", seen));
    if (!a || a.status !== "ok") return [section, absent(a, "lionheart", sample)];
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading raw cell-type scores…"));
    section.append(slot);
    artifact(sample, "lionheart", "cell-type-scores").then((data) => {
      if (data.kind !== "lionheart-scores.v1" || data.input_mode !== "experimental_single_end_ont" ||
          data.score_kind !== "sample_standardized_pearson_r" || !Array.isArray(data.scores) ||
          data.scores.length !== 898 || data.scores.some((row) =>
            !["ATAC", "DNase"].includes(row.assay) || typeof row.cell_type !== "string" ||
            (row.score !== null && (typeof row.score !== "number" || !Number.isFinite(row.score))))) {
        throw new Error("score panel is malformed");
      }
      const table = h("table", { class: "data-table" },
        h("caption", {}, "LIONHEART raw standardized correlation scores; 898 cell-type references"),
        h("thead", {}, h("tr", {}, ...["Assay", "Cell type", "Score"].map((name) => h("th", { scope: "col" }, name)))),
        h("tbody", {}, ...data.scores.map((row) => h("tr", {}, h("th", { scope: "row" }, row.assay),
          h("td", {}, row.cell_type), h("td", { class: "num" }, fmt(row.score, "correlation", { unitDisplay: false }))))));
      slot.replaceWith(h("details", {}, h("summary", {}, "All 898 raw scores"), h("div", { class: "table-wrap" }, table)));
    }).catch((error) => slot.replaceWith(h("p", { class: "problem" }, `Cell-type scores unavailable (${error.message}).`)));
    return [section];
  }

  function fragmentSignals(sample) {
    const a = algo(sample, "fragment-size");
    const seen = new Set();
    const section = h("section", { class: "panel" }, h("h2", {}, "Size density"));
    if (!a || a.status !== "ok") {
      section.append(absent(a, "fragment-size", sample), ...(a ? context(sample, "fragment-size", seen) : []));
    } else {
      const slot = h("div", {}, h("p", {}, "Loading…"));
      const short = a.metrics.find((m) => m.metric_id === "short_fraction_lt150");
      const band = sample.size_density_band || { state: "no_pinned_band", eligible_n: 0, excluded: {} };
      const exclusions = Object.entries(band.excluded || {}).map(([reason, n]) => `${reason.replace(/_/g, " ")} ${n}`).join(" · ");
      const bandStatus = band.state === "ready"
        ? `Observed cohort density min–max (n=${band.eligible_n}); descriptive, not a reference interval.`
        : band.state === "no_usable_pinned_vectors"
          ? "No cohort density band: no usable pinned size histograms (missing vectors or zero eligible 50–1000 bp molecules). Check member histograms and eligibility."
          : band.state === "cohort_too_small"
            ? `No cohort density band: ${band.eligible_n} eligible vectors of 10 needed.`
            : `No cohort density band (${String(band.state).replace(/_/g, " ")}).`;
      section.append(h("p", { class: "help" }, `${bandStatus} ${exclusions ? `Exclusions: ${exclusions}. ` : ""}167 bp marked. Short-fragment reference: ${cohortReadiness(sample, short ? [short] : [])}.`), ...context(sample, "fragment-size", seen), slot);
      artifact(sample, "fragment-size", "size-density").then((data) => {
        if (!Array.isArray(data.span_bp) || !Array.isArray(data.count) || data.span_bp.length !== 1000 ||
            data.count.length !== 1000 || data.span_bp.some((bp, i) => bp !== i + 1) ||
            data.count.some((count) => !Number.isSafeInteger(count) || count < 0)) throw new Error("size histogram invalid");
        const denominator = data.count.slice(49).reduce((total, count) => total + count, 0);
        if (!Number.isSafeInteger(denominator) || denominator === 0) throw new Error("no eligible 50–1000 bp molecules");
        const spans = data.span_bp.slice(49, 700);
        const sampleDensity = data.count.slice(49, 700).map((count) => chartPercent(count / denominator));
        const ready = band.state === "ready" && Array.isArray(band.span_bp) &&
          band.span_bp.length === spans.length && band.span_bp.every((bp, i) => bp === spans[i]) &&
          Array.isArray(band.low) && Array.isArray(band.high) &&
          band.low.length === spans.length && band.high.length === spans.length &&
          band.low.every((v, i) => Number.isFinite(v) && v >= 0 && Number.isFinite(band.high[i]) && band.high[i] >= v);
        if (band.state === "ready" && !ready) throw new Error("cohort density band invalid");
        const bandLow = ready ? band.low.map(chartPercent) : null;
        const bandHigh = ready ? band.high.map(chartPercent) : null;
        const series = ready ? [
          { type: "line", name: "Observed minimum", stack: "band", showSymbol: false, data: bandLow,
            lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 } },
          { type: "line", name: "Observed min–max", stack: "band", showSymbol: false,
            data: bandHigh.map((high, i) => Number((high - bandLow[i]).toPrecision(3))),
            lineStyle: { opacity: 0 }, areaStyle: { color: "#9eb9c9", opacity: 0.55 } },
        ] : [];
        series.push({ type: "line", name: "This sample", showSymbol: false, data: sampleDensity,
          lineStyle: { color: "#1f5f8b" }, markLine: { symbol: "none", data: [{ xAxis: "167", label: { formatter: "167 bp" } }] } });
        slot.replaceWith(chart(`Aligned-span density, 50 to 700 bp. X axis: aligned span in bp. Y axis: percent of 50–1000 bp molecules. ${bandStatus} 167 bp marked.`, {
          grid: { left: 60, right: 20, top: 20, bottom: 40 },
          xAxis: { type: "category", name: "span (bp)", data: spans.map(String), axisLabel: { interval: 99 } },
          yAxis: { type: "value", name: "% of 50–1000 bp molecules" },
          tooltip: { trigger: "axis", renderMode: "richText", formatter: (points) => {
            const index = points[0]?.dataIndex;
            if (!Number.isInteger(index)) return "";
            const lines = [`${spans[index]} bp`, `This sample: ${fmt(sampleDensity[index], "%")}`];
            if (ready) lines.push(`Cohort observed min: ${fmt(bandLow[index], "%")}`,
              `Cohort observed max: ${fmt(bandHigh[index], "%")}`);
            return lines.join("\n");
          } },
          series,
        }, dataTable("Normalized aligned-span density (% of 50–1000 bp molecules)",
          ["Span (bp)", "This sample (%)", "Cohort observed min (%)", "Cohort observed max (%)"],
          spans.map((bp, i) => [bp, fmt(sampleDensity[i], "%"), ready ? fmt(bandLow[i], "%") : "unavailable",
            ready ? fmt(bandHigh[i], "%") : "unavailable"]))));
      }).catch((e) => slot.replaceWith(h("div", { class: "problem" }, `Chart could not be drawn (${e.message}).`)));
    }
    const more = [];
    for (const id of ["end-motifs", "delfi-ratio", "long-tail", "mtdna"]) {
      const other = algo(sample, id);
      const hasChart = id === "end-motifs" || id === "delfi-ratio";
      if (!other && !(hasChart && unreadableArtifact(sample, id))) continue;
      const panel = h("section", { class: "panel" }, h("h2", {}, id),
        id === "end-motifs" || id === "delfi-ratio" ? flagPill("LEGAL") : "");
      if (!other || other.status !== "ok") {
        panel.append(absent(other, id, hasChart ? sample : null), ...(other ? context(sample, id, seen) : []));
      } else {
        panel.append(...context(sample, id, seen), h("table", { class: "data-table" },
          h("tbody", {}, ...other.metrics.filter((m) => m.value === null || typeof m.value !== "object")
            .map((m) => h("tr", {}, h("th", { scope: "row" }, m.label), h("td", {}, fmtMetric(m, { algorithmId: id })))))));
        if (id === "end-motifs") {
          const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading end-motif frequencies…"));
          panel.append(h("p", { class: "help" }, "Aligned-reference 4-mers at both fragment ends; all 256 motifs are in the data table."), slot);
          artifact(sample, id, "motif-freq").then((data) => {
            if (!Array.isArray(data.motif) || data.motif.length !== 256 ||
                !Array.isArray(data.count) || data.count.length !== 256 ||
                !Array.isArray(data.freq) || data.freq.length !== 256) throw new Error("motif panel dimensions invalid");
            if (new Set(data.motif).size !== 256 ||
                data.motif.some((motif) => typeof motif !== "string" || !/^[ACGT]{4}$/.test(motif)) ||
                data.count.some((count) => !Number.isSafeInteger(count) || count < 0) ||
                data.freq.some((freq) => !Number.isFinite(freq) || freq < 0 || freq > 1)) {
              throw new Error("motif panel values invalid");
            }
            const rows = data.motif.map((motif, i) => [motif, data.count[i], data.freq[i]]);
            const top = [...rows].sort((a, b) => b[2] - a[2] || a[0].localeCompare(b[0])).slice(0, 16);
            slot.replaceWith(chart("Sixteen most frequent aligned-end 4-mers; fraction of counted ends.", {
              grid: { left: 60, right: 20, top: 20, bottom: 45 },
              xAxis: { type: "category", name: "4-mer", data: top.map((row) => row[0]), axisLabel: { rotate: 45 } },
              yAxis: { type: "value", name: "fraction", min: 0 },
              series: [{ type: "bar", data: top.map((row) => row[2]), itemStyle: { color: "#1f5f8b" } }],
            }, dataTable("All 256 aligned-end 4-mers", ["4-mer", "Count", "Fraction"], rows)));
          }).catch((e) => slot.replaceWith(h("p", { class: "problem" }, `End-motif chart unavailable (${e.message}).`)));
        }
        if (id === "delfi-ratio") {
          const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading short/long ratio track…"));
          panel.append(h("p", { class: "help" }, "5 Mb genomic bins; short (100–150 bp) divided by long (151–220 bp), median-centred. No GC correction or classifier."), slot);
          artifact(sample, id, "ratio-track").then((data) => {
            if (!Array.isArray(data.bins) || data.bin_bp !== 5000000 || data.median_ratio_removed !== true) {
              throw new Error("ratio track contract invalid");
            }
            if (data.bins.length > 1024 || data.bins.some((bin) =>
              !bin || typeof bin.contig !== "string" || !/^(?:chr)?(?:[1-9]|1[0-9]|2[0-2])$/.test(bin.contig) ||
              !Number.isSafeInteger(bin.start) || bin.start < 0 || bin.start % data.bin_bp !== 0 ||
              !Number.isSafeInteger(bin.short) || bin.short < 0 ||
              !Number.isSafeInteger(bin.long) || bin.long < 0 ||
              (bin.long === 0
                ? bin.ratio !== null || bin.centred !== null
                : !Number.isFinite(bin.ratio) || bin.ratio < 0 || !Number.isFinite(bin.centred)))) {
              throw new Error("ratio track bin values invalid");
            }
            const labels = data.bins.map((b) => `${b.contig}:${(b.start / 1e6).toFixed(0)} Mb`);
            const values = data.bins.map((b) => b.centred);
            slot.replaceWith(chart(`Median-centred short/long ratio across ${data.bins.length} genomic bins; gaps have no long fragments.`, {
              grid: { left: 65, right: 20, top: 20, bottom: 45 },
              xAxis: { type: "category", name: "5 Mb bin (genome order)", data: labels, axisLabel: { show: false } },
              yAxis: { type: "value", name: "centred ratio" },
              series: [{ type: "line", showSymbol: false, connectNulls: false, data: values, lineStyle: { color: "#1f5f8b" } }],
            }, dataTable("DELFI-style 5 Mb bins", ["Contig", "Start (bp)", "Short", "Long", "Ratio", "Median-centred ratio"],
              data.bins.map((b) => [b.contig, b.start, b.short, b.long, b.ratio, b.centred]))));
          }).catch((e) => slot.replaceWith(h("p", { class: "problem" }, `Short/long ratio track unavailable (${e.message}).`)));
        }
      }
      more.push(panel);
    }
    return [section, ...more, ...tssProfileSignals(sample, seen), ...breakpointMotifSignals(sample, seen), ...griffinSignals(sample, seen), ...alignedEndSignals(sample, seen), ...finaletoolkitSignals(sample, seen), ...lionheartSignals(sample, seen)];
  }

  function griffinSignals(sample, seen) {
    const a = algo(sample, "griffin");
    if (!a && !unreadableArtifact(sample, "griffin")) return [];
    const section = h("section", { class: "panel" },
      h("div", { class: "findings-head" }, h("h2", {}, "Griffin TF-site profile"), flagPill("LEGAL")));
    if (!a || a.status !== "ok") return [section, absent(a, "griffin", sample)];
    section.append(h("p", { class: "help" }, "GC-corrected nucleosome protection from the pinned Griffin workflow on a compatible paired-end WGS BAM. The primary TF list is an operator-registered choice; this is not a TF expression call."));
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading Griffin profile…"));
    section.append(...context(sample, "griffin", seen), slot);
    artifact(sample, "griffin", "tf-site-profile").then((data) => {
      const profiles = Array.isArray(data.profiles) ? data.profiles : [];
      const primary = profiles.find((profile) => profile.site_name === data.primary_site);
      if (!primary || !Array.isArray(primary.bin_start_bp) || !Array.isArray(primary.gc_corrected_coverage) ||
          primary.bin_start_bp.length !== primary.gc_corrected_coverage.length || primary.bin_start_bp.length < 3) {
        throw new Error("primary Griffin profile is incomplete");
      }
      const step = primary.bin_start_bp[1] - primary.bin_start_bp[0];
      const points = primary.bin_start_bp.map((start, i) => [start + step / 2, primary.gc_corrected_coverage[i]]);
      const rows = profiles.map((profile) => [profile.site_name, profile.site_count, profile.central_coverage, profile.amplitude]);
      slot.replaceWith(
        h("p", {}, `Primary TF list: ${data.primary_site}; ${fmtCount(primary.site_count)} sites. The table preserves upstream bin starts and lists the exact centers plotted on the x-axis.`),
        chart(`GC-corrected Griffin nucleosome coverage at ${data.primary_site} sites.`, {
          grid: { left: 65, right: 20, top: 20, bottom: 45 },
          xAxis: { type: "value", name: "bp from site" }, yAxis: { type: "value", name: "relative coverage" },
          series: [{ type: "line", showSymbol: false, data: points, lineStyle: { color: "#1f5f8b" } }],
        }, dataTable(`Griffin ${data.primary_site} corrected profile`, ["Bin start (bp)", "Plotted bin center (bp)", "Relative coverage"],
          primary.bin_start_bp.map((start, i) => [start, start + step / 2, primary.gc_corrected_coverage[i]]))),
        dataTable("Griffin TF-site lists", ["Site list", "Sites", "Central coverage", "Amplitude"], rows),
      );
    }).catch((error) => slot.replaceWith(h("p", { class: "problem" }, `Griffin profile unavailable (${error.message}).`)));
    return [section];
  }

  function finaletoolkitSignals(sample, seen) {
    const a = algo(sample, "finaletoolkit-xcheck");
    if (!a && !unreadableArtifact(sample, "finaletoolkit-xcheck")) return [];
    const section = h("section", { class: "panel" }, h("h2", {}, "FinaleToolkit paired-end cross-check"), flagPill("LEGAL"));
    if (!a || a.status !== "ok") return [section, absent(a, "finaletoolkit-xcheck", sample)];
    section.append(h("p", { class: "help" }, "Engineering parity on a verified paired-end Illumina BAM. Neither the single-end ONT results nor the published GC-corrected DELFI classifier are compared here."),
      ...context(sample, "finaletoolkit-xcheck", seen));
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading paired-end cross-check…"));
    section.append(slot);
    artifact(sample, "finaletoolkit-xcheck", "finaletoolkit-xcheck").then((data) => {
      const correlation = (value) => value === null || (typeof value === "number" && Number.isFinite(value) && value >= -1 && value <= 1);
      const motifDifference = data.motif?.max_abs_frequency_difference;
      if (data.schema !== "traceback.lab.finaletoolkit-xcheck.v1" || data.version !== "1.1.0" ||
          !/^[0-9a-f]{64}$/.test(data.runtime_pin_sha256 || "") ||
          !correlation(data.motif?.r) || !correlation(data.delfi?.r) ||
          (motifDifference !== undefined && (typeof motifDifference !== "number" || !Number.isFinite(motifDifference) || motifDifference < 0 || motifDifference > 1)) ||
          !Number.isInteger(data.delfi?.comparable_bins) || data.delfi.comparable_bins < 0 ||
          !Number.isInteger(data.pairs) || data.pairs < 0 ||
          !["concordant", "discordant", "insufficient_variation"].includes(data.verdict)) {
        throw new Error("cross-check artifact is malformed");
      }
      slot.replaceWith(
        h("p", {}, `Result: ${data.verdict.replace(/_/g, " ")}; ${data.pairs.toLocaleString("en-US")} paired molecules; runtime pin ${data.runtime_pin_sha256.slice(0, 12)}.`),
        dataTable("Paired-end method concordance", ["Comparison", "Pearson r", "Engineering threshold", "Support"], [
          ["4-mer end frequencies", fmt(data.motif.r, "correlation", { unitDisplay: false }), "≥0.95", "256 motifs"],
          ["Uncorrected 100-kb short/long ratios", fmt(data.delfi.r, "correlation", { unitDisplay: false }), "≥0.90", `${data.delfi.comparable_bins} bins with long fragments`],
        ]),
        h("p", { class: "help" }, `Maximum absolute 4-mer frequency difference: ${motifDifference === undefined ? "not recorded" : motifDifference.toExponential(2)}. Pearson compares patterns and does not establish absolute frequency equality. Exact short/long count agreement: ${data.delfi.upstream_counts_equal ? "yes" : "no"}. ${data.note}`),
      );
    }).catch((error) => slot.replaceWith(h("p", { class: "problem" }, `Paired-end cross-check unavailable (${error.message}).`)));
    return [section];
  }

  function tssProfileSignals(sample, seen) {
    const a = algo(sample, "tss-profile");
    if (!a && !unreadableArtifact(sample, "tss-profile")) return [];
    const section = h("section", { class: "panel" }, h("h2", {}, "TSS coverage profile"));
    if (!a || a.status !== "ok") return [section, absent(a, "tss-profile", sample)];
    const ratio = a.metrics.find((m) => m.metric_id === "central_flank_ratio");
    const flags = ratio && ratio.entry ? ratio.entry.flags || [] : [];
    section.append(h("p", { class: "help" }, "GENCODE v46 protein-coding gene TSSs; 50 bp strand-oriented bins across ±1 kb, plotted at bin centres. Central −150 to +50 bp coverage divided by outer-flank coverage. Descriptive gene-set profile only; no expression call or clinical cutoff."),
      h("p", {}, `Central/flank ratio: ${ratio ? fmtMetric(ratio, { algorithmId: "tss-profile" }) : "—"}. ${ratio ? ratio.caption || "" : ""}`),
      ...flags.map((flag) => flagPill(flag)),
      ...context(sample, "tss-profile", seen));
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading TSS coverage…"));
    section.append(slot);
    artifact(sample, "tss-profile", "tss-coverage").then((data) => {
      if (!Array.isArray(data.offset_bp) || !Array.isArray(data.coverage_x) ||
          data.offset_bp.length !== 40 || data.coverage_x.length !== 40 ||
          !data.offset_bp.every((offset, index) => offset === -1000 + index * 50) ||
          !data.coverage_x.every((value) => Number.isFinite(value) && value >= 0) ||
          !Number.isInteger(data.gene_n) || data.gene_n < 1 ||
          !Number.isInteger(data.eligible_autosomal_reads) ||
          !/^[0-9a-f]{64}$/.test(data.annotation_sha256 || "")) {
        throw new Error("TSS panel dimensions or provenance invalid");
      }
      const points = data.offset_bp.map((offset, i) => [offset + 25, data.coverage_x[i]]);
      const rows = data.offset_bp.map((offset, i) => [`[${offset}, ${offset + 50})`, data.coverage_x[i]]);
      slot.replaceWith(h("p", { class: "help" }, `${data.gene_n.toLocaleString("en-US")} complete protein-coding gene windows; ${data.eligible_autosomal_reads.toLocaleString("en-US")} eligible autosomal reads. Annotation ${data.annotation_sha256.slice(0, 12)}.`),
        chart("Mean aligned-fragment coverage across strand-oriented protein-coding TSS windows", {
          grid: { left: 60, right: 20, top: 20, bottom: 40 },
          xAxis: { type: "value", name: "bp from TSS (bin centre)", min: -1000, max: 1000 },
          yAxis: { type: "value", name: "mean coverage ×" },
          series: [{ type: "line", showSymbol: false, data: points, lineStyle: { color: "#1f5f8b" } }],
        }, dataTable("TSS coverage in 50 bp bins", ["Bin interval (bp)", "Mean coverage ×"], rows)));
    }).catch((error) => slot.replaceWith(h("div", { class: "problem" }, `TSS profile could not be drawn (${error.message}).`)));
    return [section];
  }

  function copyNumberSignals(sample) {
    const a = algo(sample, "copy-number");
    const tf = value(sample, "copy-number", "tumor_fraction");
    const counted = value(sample, "copy-number", "counted_reads");
    const pon = value(sample, "copy-number", "pon_policy");
    const section = h("section", { class: "panel" }, h("div", { class: "findings-head" }, h("h2", {}, "Genome log2 track"), flagPill("FDA")));
    if (!a || a.status !== "ok") return [section, absent(a, "copy-number", sample)];
    section.append(h("p", {}, `Tumour fraction ${fmt(tf, "fraction")} · LoD 3% established at 0.1x Illumina with PoN; this run: ${pon === "none" ? "no PoN" : "PoN"}, ${fmtCount(counted)} reads. Not interpretable without a protocol-matched panel of normals.`));
    const slot = h("div", {}, h("p", {}, "Loading…"));
    section.append(...context(sample, "copy-number", new Set()), slot);
    artifact(sample, "copy-number", "genome-track").then((data) => {
      const bins = data.bins.filter((b) => b.log2 !== null);
      const offsets = {};
      let cursor = 0;
      for (const b of data.bins) { if (!(b.chr in offsets)) offsets[b.chr] = cursor; cursor = Math.max(cursor, offsets[b.chr] + b.end); }
      const points = bins.map((b) => [(offsets[b.chr] + b.start) / 1e6, b.log2]);
      slot.replaceWith(chart(`Copy-number log2 ratio across the genome, ${fmtCount(bins.length)} bins; tumour fraction ${fmt(tf, "fraction")}.`, {
        grid: { left: 50, right: 20, top: 20, bottom: 40 }, xAxis: { type: "value", name: "Mb (genome order)" }, yAxis: { type: "value", name: "log2", min: -2, max: 2 },
        series: [{ type: "scatter", symbolSize: 3, data: points, itemStyle: { color: "#1f5f8b" } }],
      }, dataTable("Copy-number bins", ["Contig", "Start", "End", "log2", "Mask"], data.bins.map((b) => [b.chr, b.start, b.end, b.log2, b.mask || "—"]))));
    }).catch((e) => slot.replaceWith(h("div", { class: "problem" }, `Chart could not be drawn (${e.message}).`)));
    return [section];
  }

  function fragXMethSignals(sample) {
    const a = algo(sample, "frag-x-meth");
    const section = h("section", { class: "panel" }, h("h2", {}, "Fragment size × methylation"));
    if (!a || a.status !== "ok") return a || unreadableArtifact(sample, "frag-x-meth")
      ? [section, absent(a, "frag-x-meth", sample)] : [];
    const delta = value(sample, "frag-x-meth", "short_fragment_hypomethylation_delta");
    section.append(
      h("p", {}, "Each molecule contributes once after at least four distinct CpG calls. The matrix is descriptive; no normal range is claimed."),
      h("p", {}, "Long (151–220 bp) mean minus short (100–150 bp) mean: ",
        h("strong", {}, fmt(delta, "fraction", { role: "difference" })),
        delta === null ? " (one or both groups have no eligible molecules)." : ". A positive value means the short fragments have lower 5mC fraction."),
      ...context(sample, "frag-x-meth", new Set()),
    );
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading size × methylation counts…"));
    section.append(slot);
    artifact(sample, "frag-x-meth", "size-methylation").then((data) => {
      if (!Array.isArray(data.size_edges_bp) || !Array.isArray(data.methylation_edges)
          || !Array.isArray(data.counts) || data.counts.length !== data.size_edges_bp.length
          || data.counts.some((row) => !Array.isArray(row) || row.length !== data.methylation_edges.length - 1)) {
        throw new Error("panel dimensions invalid");
      }
      const methylLabels = data.methylation_edges.slice(0, -1).map((edge, i) =>
        `${Math.round(edge * 100)}–${Math.round(data.methylation_edges[i + 1] * 100)}% ${i === data.methylation_edges.length - 2 ? "(inclusive)" : ""}`);
      const sizeLabels = data.size_edges_bp.map((edge, i) =>
        i + 1 === data.size_edges_bp.length ? `${edge}+ bp` : `${edge}–${data.size_edges_bp[i + 1] - 1} bp`);
      const table = h("table", { class: "data-table" },
        h("caption", {}, `Molecules by aligned size and 5mC fraction; ${fmtCount(data.n)} eligible molecules`),
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Aligned size"),
          ...methylLabels.map((label) => h("th", { scope: "col" }, label)))),
        h("tbody", {}, ...data.counts.map((row, i) =>
          h("tr", {}, h("th", { scope: "row" }, sizeLabels[i]),
            ...row.map((count) => h("td", { class: "num" }, fmtCount(count)))))));
      slot.replaceWith(
        h("p", { class: "help" }, `Short group n = ${fmtCount(data.short_n)}, mean = ${fmt(data.short_mean, "fraction")}; long group n = ${fmtCount(data.long_n)}, mean = ${fmt(data.long_mean, "fraction")}. A missing mean is not zero.`),
        h("div", { class: "table-wrap" }, table),
      );
    }).catch((error) => slot.replaceWith(h("p", { class: "problem" }, `Size × methylation table unavailable (${error.message}). The scalar result remains above.`)));
    return [section];
  }

  function methylationSignals(sample) {
    const ids = ["global-5mc", "global-5hmc", "pmd-score", "hypometh-reads", "modbase-qc"];
    const present = ids.map((id) => algo(sample, id)).filter(Boolean);
    const joint = fragXMethSignals(sample);
    const seen = new Set();
    if (!present.length && !joint.length) return [h("p", { class: "empty" }, "No methylation summary algorithm has run on this sample yet (Phase 1). Cell-type composition is in Body map.")];
    return [...joint, ...present.map((a) => h("section", { class: "panel" }, h("h2", {}, a.algorithm_id), a.status === "ok"
      ? h("table", { class: "data-table" }, h("tbody", {}, ...a.metrics.filter((m) => m.value === null || typeof m.value !== "object").map((m) => h("tr", {}, h("th", { scope: "row" }, m.label), h("td", {}, fmtMetric(m, { algorithmId: a.algorithm_id })), h("td", {}, stateChip(m))))))
      : absent(a, a.algorithm_id), ...context(sample, a.algorithm_id, seen)))];
  }


  // -- Body map: tissue composition with intervals (no donuts) -------------------------------------
  const ORGANS = [
    ["Blood", (c) => c.startsWith("Blood-") || c === "Megakaryocytes" || c === "Eryth-prog"],
    ["Liver and gut", (c) => /^(Liver|Gallbladder|Colon|Small-Int|Gastric|Pancreas)/.test(c)],
    ["Vascular, heart and lung", (c) => /^(Endothel|Heart|Lung|Smooth-Musc)/.test(c)],
    ["Other", () => true],
  ];
  function bodyMap(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Body map`),
      h("div", { class: "findings-head" }, h("h1", { class: "view-title" }, "Body map"), flagPill("LEGAL", "FLAG: LEGAL — research-only licenses; atlas terms unknown."))];
    const a = algo(sample, "cell-origin");
    if (!a || a.status !== "ok") { out.push(absent(a, "cell-origin", sample)); return out; }
    out.push(h("p", { class: "help" }, `Reference cohort ${sample.cohort_version || "not built"} (${sample.cohort_n} members)${sample.cohort_kind === "provisional_external" ? "; protocol-mismatched external reference" : ""}. Tissue references use eligible comparable samples after leave-one-out: 10–29 show observed min–max; 30+ show the 90% bootstrap interval of the 95th-percentile cutoff. The diamond marks the eligible mean, which need not lie inside a cutoff interval. Minor tissues are not interpretable below 5x; their estimate and interval stay visible.`));
    out.push(...context(sample, "cell-origin", new Set()));
    const rows = a.metrics.filter((m) => m.metric_id.startsWith("fraction_"));
    const cohortSummary = (m) => m.eligible_n >= 10 && m.cohort_mean !== null && Array.isArray(m.interval)
      ? { mean: m.cohort_mean, low: m.interval[0], high: m.interval[1] } : null;
    const top = Math.min(1, Math.max(0.05, ...rows.map((m) => Math.max(m.ci ? m.ci[1] : m.value, (cohortSummary(m) || {}).high || 0))));
    const level = (rows.find((m) => m.ci_level) || {}).ci_level;
    const intervalName = level ? `${Math.round(level * 100)}% bootstrap interval` : "interval";
    const groups = ORGANS.map(([name]) => [name, []]);
    for (const m of rows) {
      const contributor = m.label.split(" share")[0];
      const index = ORGANS.findIndex(([, test]) => test(contributor));
      groups[index][1].push(Object.assign({ contributor }, m));
    }
    for (const [name, members] of groups) {
      if (!members.length) continue;
      members.sort((x, y) => y.value - x.value);
      const w = 560, rowH = 22, left = 170, hgt = members.length * rowH + 40;
      const x = (v) => left + (v / top) * (w - left - 20);
      const axisY = members.length * rowH + 22;
      const ticks = [0, top / 2, top].map((t) => svg("text", { x: x(t), y: axisY + 12, "text-anchor": "middle", class: "axis" }, `${Math.round(t * 100)}%`));
      const marks = members.flatMap((m, i) => {
        const y = 14 + i * rowH;
        const out2 = [svg("text", { x: left - 6, y: y + 4, "text-anchor": "end", class: "axis" }, m.contributor)];
        const cohort = cohortSummary(m);
        if (cohort) {
          out2.push(svg("line", { x1: x(cohort.low), x2: x(cohort.high), y1: y - 5, y2: y - 5, class: "cohort-band" }));
          out2.push(svg("text", { x: x(cohort.mean), y: y - 7, "text-anchor": "middle", class: "cohort-mean" }, "◆"));
        }
        if (m.ci) out2.push(svg("line", { x1: x(m.ci[0]), x2: x(m.ci[1]), y1: y, y2: y, class: "ci" }));
        out2.push(svg("text", { x: x(m.value), y: y + 5, "text-anchor": "middle", class: m.state === "not_interpretable" ? "pt-open" : "pt" }, m.state === "not_interpretable" ? "▨" : "●"));
        return out2;
      });
      const label = `${name}: ${members.length} cell types; dots are this sample, diamonds are eligible cohort means, upper lines are observed min–max for 10–29 eligible values or the 90% bootstrap interval of the 95th-percentile cutoff for 30 or more, lower lines are sample intervals. The mean may lie outside a cutoff interval. Hatched dots are not interpretable at this depth.`;
      out.push(h("section", { class: "panel" }, h("h2", {}, name),
        h("p", { class: "help" }, "● Sample share · lower line: sample bootstrap interval · ◆ eligible cohort mean · upper line: cohort reference interval (observed min–max at n = 10–29; 90% bootstrap interval of the 95th-percentile cutoff at n ≥ 30)."),
        svg("svg", { viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label, class: "composition-svg" }, svg("title", {}, label), ...marks,
          svg("line", { x1: left, x2: w - 20, y1: axisY, y2: axisY, class: "ci" }), ...ticks),
        dataTable(`${name} (${intervalName})`, ["Cell type", "Share", "Interval low", "Interval high", "Cohort mean", "Reference low", "Reference high", "Reference interval meaning", "Eligible n", "State"],
          members.map((m) => { const cohort = cohortSummary(m);
            const cell = (value) => rawCell(fmtMetricValue(m, value), value, m.unit);
            return [m.contributor, rawCell(fmtMetric(m), m.value, m.unit),
            cell(m.ci ? m.ci[0] : null), cell(m.ci ? m.ci[1] : null),
            cell(cohort ? cohort.mean : null), cell(cohort ? cohort.low : null),
            cell(cohort ? cohort.high : null),
            cohort ? (m.exploratory_n ? "Observed min–max" : "90% bootstrap interval of the 95th-percentile cutoff") : "—", fmtCount(m.eligible_n),
            (STATE[m.state] || ["", m.state])[1] + (m.reason ? ` (${m.reason})` : "")]; }))));
    }
    out.push(h("p", { class: "help" }, "Groups are an editorial arrangement of atlas cell types by organ system, not an inferred tissue of origin."));
    return out;
  }

  // -- Agreement: paired signals only, no combined score (D29) ----------------------------------------
  function agreement(sample) {
    const report = sample.concordance || { pairs: [] };
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Agreement`), h("h1", { class: "view-title" }, "Agreement"),
      h("p", { class: "status-line" }, report.caption || "These signals share inputs and are not independent confirmation; agreement is not validation."),
      h("p", { class: "help" }, report.wgd_caveat || "Copy-number tumour fraction can shift with whole-genome doubling.")];
    const tf = value(sample, "copy-number", "tumor_fraction");
    const mode = value(sample, "fragment-size", "mode_bp");
    const mt = value(sample, "mtdna", "chrm_fraction");
    const pairs = [
      ["size-cn", "Size mode vs copy-number TF", "fragment-size mode (bp)", mode, "bp", "tumour fraction", tf, "fraction"],
      ["mtdna-cn", "mtDNA fraction vs copy-number TF", "chrM fraction", mt, "fraction", "tumour fraction", tf, "fraction"],
    ];
    for (const [pairId, title, xLabel, xValue, xUnit, yLabel, yValue, yUnit] of pairs) {
      const section = h("section", { class: "panel" }, h("h2", {}, title), flagPill("FDA"));
      const cohort = (sample.agreement_pairs || []).find((row) => row.pair_id === pairId);
      const points = cohort && cohort.state === "ready" ? cohort.points : [];
      const cohortText = points.length
        ? `${points.length} compatible paired cohort ${points.length === 1 ? "source" : "sources"} from ${cohort.cohort_version}; this source is excluded. Descriptive points, not a reference interval.`
        : `Cohort points unavailable (${cohort ? cohort.state.replace(/_/g, " ") : "no pinned pair data"}).`;
      if (xValue === null || yValue === null) {
        section.append(h("p", { class: "empty" }, `Needs both signals at full depth (${xValue === null ? xLabel : yLabel} missing). ${cohortText}`));
      } else {
        const plotX = (v) => xUnit === "fraction" ? chartPercent(v) : v;
        const plotY = (v) => yUnit === "fraction" ? chartPercent(v) : v;
        const xAxisLabel = xUnit === "fraction" ? `${xLabel} (%)` : xLabel;
        const yAxisLabel = yUnit === "fraction" ? `${yLabel} (%)` : yLabel;
        section.append(h("p", { class: "help" }, cohortText), chart(`${title}: this sample at ${fmt(xValue, xUnit)} and ${fmt(yValue, yUnit)}; ${cohortText}`, {
          grid: { left: 60, right: 20, top: 20, bottom: 40 }, xAxis: { type: "value", name: xAxisLabel, scale: true }, yAxis: { type: "value", name: yAxisLabel, min: 0 },
          tooltip: { trigger: "item", renderMode: "richText", formatter: (point) =>
            `${xLabel}: ${fmt(point.value[0], xUnit === "fraction" ? "%" : xUnit)}\n${yLabel}: ${fmt(point.value[1], yUnit === "fraction" ? "%" : yUnit)}` },
          series: [
            { type: "scatter", name: "Paired cohort", symbolSize: 6, data: points.map((p) => [plotX(p.x), plotY(p.y)]), itemStyle: { color: "#7892a1" } },
            { type: "scatter", name: "This sample", symbolSize: 12, data: [[plotX(xValue), plotY(yValue)]], itemStyle: { color: "#8a4b08" } },
          ],
        }, dataTable(`${title}; ${cohortText}`, ["Sample", xAxisLabel, yAxisLabel],
          [[`${cohort?.target_display_id || sample.id12} (this sample)`,
            rawCell(fmt(xValue, xUnit), xValue, xUnit), rawCell(fmt(yValue, yUnit), yValue, yUnit)],
            ...points.map((p) => [p.display_id || p.source_id,
              rawCell(fmt(p.x, xUnit), p.x, xUnit), rawCell(fmt(p.y, yUnit), p.y, yUnit)])])));
      }
      out.push(section);
    }
    const methPair = (report.pairs || []).find((pair) => pair.id === "cn-meth");
    if (methPair) {
      const section = h("section", { class: "panel" }, h("h2", {}, methPair.label), flagPill("FDA"), flagPill("CLIA"));
      const reasons = {
        no_pon: "A verified panel of normals is needed before copy-number tumour fraction can be compared.",
        missing_cn: "Copy-number tumour fraction is unavailable.",
        cn_binding_unavailable: "The selected cohort cannot verify this sample's copy-number result. Check cohort status and reload.",
        stale_cn_binding: "This copy-number result does not match the selected cohort binding. Rerun this sample with the current cohort before comparing.",
        unidentifiable_cn: "Copy-number tumour fraction was not identifiable.",
        missing_meth_tf: "Methylation tumour fraction is unavailable; inspect the registered marker reference and method result.",
        unidentifiable_meth_tf: "The observed markers did not identify a methylation tumour fraction.",
      };
      if (methPair.support && methPair.support.markers_covered !== null) {
        const support = methPair.support;
        const residual = support.fit_rms === null
          ? "Fit residual unavailable."
          : `Unnormalized weight-fit residual RMS ${fmt(support.fit_rms, "fraction", { role: "difference" })}.`;
        const exactFitCaveat = support.markers_covered === 2
          ? " With two covered markers, an interior fit is exactly determined; zero residual does not establish fit quality." : "";
        section.append(h("p", { class: "help" },
          `Methylation numerical fit: ${fmtCount(support.markers_covered)} covered markers, ${fmtCount(support.cpg_calls_used)} CpG calls; condition number ${support.reference_condition_number === null ? "unavailable" : fmt(support.reference_condition_number, null)}. ${residual}${exactFitCaveat} These counts do not establish biological calibration.`));
        if (support.markers_covered === 0)
          section.append(h("p", { class: "problem" }, "No registered CpG marker has a usable C+m call on this input. Check zero-based CpG-C positions, the genome reference, and modification-call coverage."));
      }
      if (methPair.state !== "ok") {
        const unavailable = methPair.unavailable_reasons?.length ? methPair.unavailable_reasons : [methPair.state];
        section.append(h("p", { class: "empty" }, unavailable.map(reason => reasons[reason] || "This pair is unavailable.").join(" ")));
      } else {
        section.append(chart(`${methPair.label}: this sample at ${fmt(methPair.x, "fraction")} and ${fmt(methPair.y, "fraction")}.`, {
          grid: { left: 60, right: 20, top: 20, bottom: 40 }, xAxis: { type: "value", name: `${methPair.x_label} (%)`, scale: true }, yAxis: { type: "value", name: `${methPair.y_label} (%)`, min: 0 },
          tooltip: { trigger: "item", renderMode: "richText", formatter: (point) =>
            `${methPair.x_label}: ${fmt(point.value[0], "%")}\n${methPair.y_label}: ${fmt(point.value[1], "%")}` },
          series: [{ type: "scatter", symbolSize: 12, data: [[chartPercent(methPair.x), chartPercent(methPair.y)]], itemStyle: { color: "#8a4b08" } }],
        }, dataTable(methPair.label, ["Sample", `${methPair.x_label} (%)`, `${methPair.y_label} (%)`],
          [[sample.id12, rawCell(fmt(methPair.x, "fraction"), methPair.x, "fraction"),
            rawCell(fmt(methPair.y, "fraction"), methPair.y, "fraction")]])));
      }
      out.push(section);
    }
    out.push(...context(sample, "copy-number", new Set()));
    return out;
  }

  // -- Compare: each response is pinned to one immutable cohort generation ---------
  function recoveryNotice(subject, problem) {
    const detail = problem || {};
    return h("div", { class: "problem" },
      h("p", { class: "problem-title" }, `${subject}: ${detail.summary || "unavailable"}`),
      detail.code ? h("p", {}, `Code ${detail.code}`) : "",
      detail.cause ? h("p", {}, detail.cause) : "",
      h("p", {}, h("strong", {}, "Next action: "), detail.fix || "Reload Compare and check the Lab run log."),
      detail.docs ? h("p", { class: "help" }, `Guide: ${detail.docs}`) : "");
  }

  function compare(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Compare`),
      h("div", { class: "findings-head" }, h("h1", { class: "view-title" }, "Compare"), flagPill("CLIA")),
      h("p", { class: "help" }, "Compared with the selected cohort: an exploratory distribution, not a healthy or clinical reference. Only eligible, compatible, status-ok results count; a scored member is removed before the sample-size gate.")];
    const shell = h("section", { class: "panel", "aria-labelledby": "compare-title" },
      h("h2", { id: "compare-title" }, "Cohort generation"));
    const slot = h("div", { "aria-live": "polite" }, h("p", {}, "Loading cohorts…"));
    shell.append(slot);
    out.push(shell);
    let request = 0;
    let results = null;
    const show = (cohort) => {
      const current = ++request;
      results.replaceChildren(h("p", {}, "Loading comparison…"));
      fetch(`/api/cohorts/${encodeURIComponent(cohort.cohort_id)}/samples/${sample.id12}/compare`, { credentials: "same-origin" })
        .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
        .then((data) => {
          if (!results.isConnected || current !== request) return;
          if (data.state === "cohort_unavailable" || data.state === "target_unavailable") {
            results.replaceChildren(recoveryNotice(data.state === "target_unavailable" ? "This sample" : `Cohort ${cohort.cohort_id}`, data.problem));
            return;
          }
          const nodes = [
            h("p", {}, `Generation ${data.cohort_version} · ${data.member_n} distinct members · PoN n = ${data.pon_n} (separate denominator).`),
          ];
          if (data.cohort_version !== cohort.cohort_version) nodes.push(h("p", { class: "status-line" }, "A newer cohort generation was published after the list loaded. This comparison uses the generation named above; reload to refresh the list."));
          if (data.kind === "provisional_external") nodes.push(h("p", { class: "status-line" }, "Provisional external reference: protocol mismatch. Interpret ranges cautiously."));
          if (data.state === "example_unscorable") {
            nodes.push(h("p", { class: "empty" }, "EXAMPLE (synthetic) cannot be scored against a real kit cohort."));
            results.replaceChildren(...nodes);
            return;
          }
          if (data.state === "stale_target") {
            nodes.push(h("p", { class: "status-line" }, "The sample changed while Compare loaded. Reload to compare its current full input."));
            results.replaceChildren(...nodes);
            return;
          }
          if (data.pon_problem) nodes.push(h("p", { class: "problem" }, `Panel of normals unavailable: copy-number and dependent comparisons are withheld; independent metrics remain comparable. ${data.pon_problem.fix}`));
          nodes.push(h("p", {}, `Target aligner: ${data.target_aligner}. Eligible members are counted separately for each metric after self-exclusion.`));
          if (data.target_aligner.startsWith("pre-aligned; provenance unknown")) nodes.push(h("p", { class: "status-line" },
            "Pre-aligned processing provenance is unknown. The cohort store isolates these inputs by input identity; they are not pooled as protocol-matched references."));
          const alignerRows = Object.entries(data.aligner_counts || {}).map(([name, count]) =>
            h("tr", {}, h("th", { scope: "row" }, name), h("td", { class: "num" }, fmtCount(count))));
          const alignerTable = h("table", { class: "data-table" },
            h("caption", {}, "Generation members by aligner identity"),
            h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Aligner"), h("th", { scope: "col" }, "Members"))),
            h("tbody", {}, ...alignerRows));
          nodes.push(h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Generation aligner counts" }, alignerTable));
          if (!data.metrics.length) nodes.push(h("p", { class: "empty" }, "No current compatible cohort-reference metric is available for this sample. Missing or failed metrics are not zero."));
          else {
            const rows = data.metrics.map((metric) => {
              const exclusions = Object.entries(metric.excluded || {}).map(([reason, count]) => `${reason.replace(/_/g, " ")}: ${count}`).join("; ") || "none";
              const alignerCounts = Object.entries(metric.aligner_counts || {}).map(([name, count]) => `${name}: ${count}`).join("; ") || "none";
              const original = metric.original_reference || {};
              const sourceReference = !metric.original_reference ? "no library entry" :
                `${original.kind || "none"}${original.source ? ": " + original.source : ""}`;
              const evidence = metric.entry || {};
              const publishedValue = original.kind === "published_mean" ? `Published mean: ${original.value} ${original.unit || ""}` :
                original.kind === "published_range" ? `Published range: ${original.lo}–${original.hi} ${original.unit || ""}` : "No published numeric range";
              const citations = (evidence.citations || []).map((citation) => citation.startsWith("doi:") ?
                h("a", { href: `https://doi.org/${encodeURIComponent(citation.slice(4))}`, rel: "noreferrer noopener", target: "_blank" }, citation) : citation);
              const publishedEvidence = !metric.original_reference ? sourceReference : h("details", {},
                h("summary", {}, sourceReference),
                h("p", {}, publishedValue, original.statistic ? ` (${original.statistic})` : ""),
                h("p", {}, "Original evidence tier: ", tierBadge(evidence.origin_tier || metric.tier),
                  evidence.origin_platform ? ` · ${evidence.origin_platform}` : ""),
                h("p", {}, "Citations: ", citations.length ? citations.flatMap((citation, i) => i ? [", ", citation] : [citation]) : "none",
                  ...spacedUnverifiedBadge(metric.entry)));
              const values = metric.cohort_values || [];
              const valueTable = values.length ? h("details", {},
                h("summary", {}, `Eligible values (${values.length})`),
                h("table", { class: "data-table" }, h("caption", {}, "Anonymous compatible cohort members after self-exclusion"),
                  h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Member"), h("th", { scope: "col" }, "Value"))),
                  h("tbody", {}, ...values.map((value, i) => h("tr", {}, h("th", { scope: "row" }, String(i + 1)), h("td", {}, fmtMetricValue(metric, value))))))) : "";
              return h("tr", {},
                h("th", { scope: "row" }, metric.label),
                h("td", {}, fmtMetric(metric)),
                h("td", {}, stateChip(metric), strip(metric), valueTable),
                h("td", { class: "num" }, fmtCount(metric.eligible_n)),
                h("td", {}, alignerCounts),
                h("td", {}, exclusions),
                h("td", {}, publishedEvidence));
            });
            nodes.push(h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Comparison metrics" }, h("table", { class: "data-table" },
              h("caption", {}, `Comparison against ${data.cohort_version}; each n is post-exclusion`),
              h("thead", {}, h("tr", {}, ...["Metric", "This sample", "Selected cohort interpretation", "Eligible n", "Eligible aligners", "Excluded", "Original library reference"].map((name) => h("th", { scope: "col" }, name)))),
              h("tbody", {}, ...rows))));
          }
          if (data.unscored && data.unscored.length) {
            nodes.push(h("p", { class: "help" }, `Not scored: ${data.unscored.map((item) => `${item.algorithm_id} (${item.status}: ${item.reason})`).join("; ")}. No value was substituted.`));
          }
          results.replaceChildren(...nodes);
        })
        .catch((error) => {
          if (results.isConnected && current === request) results.replaceChildren(h("p", { class: "problem" }, `Comparison could not be loaded (${error.message}). Check the Lab server, then reload.`));
        });
    };
    fetch("/api/cohorts", { credentials: "same-origin" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((data) => {
        if (!slot.isConnected) return;
        const unavailable = data.unavailable || [];
        const rootProblem = data.problem ? recoveryNotice("Cohort storage", data.problem) : "";
        const unavailablePanel = unavailable.length ? h("section", { class: "panel", "aria-labelledby": "compare-unavailable-title" },
          h("h3", { id: "compare-unavailable-title" }, "Unavailable cohorts"),
          h("p", { class: "help" }, "These cohorts cannot be selected. Other published generations remain available."),
          ...unavailable.map((row) => recoveryNotice(`Cohort ${row.cohort_id}`, row.problem))) : "";
        if (!data.cohorts.length) {
          slot.replaceChildren(h("p", { class: "empty" }, data.problem || unavailable.length ?
            "No selectable cohort generation is available. Follow the recovery action below, then reload Compare." :
            "No cohort generation has been built under this ROOT. Add eligible samples with `traceback lab cohort add`, then publish with `traceback lab cohort build`."), rootProblem, unavailablePanel);
          return;
        }
        const picker = h("select", { "aria-label": "Cohort generation", "data-requires-server": "" },
          ...data.cohorts.map((row) => h("option", { value: row.cohort_id }, `${row.cohort_id} · ${row.cohort_version} · n = ${row.member_n}`)));
        results = h("div", { "aria-live": "polite" });
        slot.replaceChildren(h("label", {}, "Choose cohort ", picker), rootProblem, unavailablePanel, results);
        // The selected generation is read once by the API for every metric.
        picker.addEventListener("change", () => show(data.cohorts.find((row) => row.cohort_id === picker.value)));
        show(data.cohorts[0]);
      })
      .catch((error) => {
        if (slot.isConnected) slot.replaceChildren(h("p", { class: "problem" }, `Cohorts could not be loaded (${error.message}). Check the Lab server, then reload.`));
      });
    out.push(sampleCompare(sample));
    return out;
  }

  function sampleOverlay(metric) {
    const a = metric.value_a, b = metric.value_b;
    const low = Math.min(0, a, b), high = Math.max(0, a, b);
    const span = high - low || 1;
    const x = (value) => 10 + ((value - low) / span) * 160;
    const label = `${metric.label}: sample A ${fmtMetricValue(metric, a)}, sample B ${fmtMetricValue(metric, b)}, B minus A ${fmtMetricValue(metric, metric.delta_b_minus_a, { role: "difference" })}. Local display scale only; no reference threshold.`;
    return h("figure", { class: "sample-overlay" },
      svg("svg", { viewBox: "0 0 180 30", role: "img", "aria-label": label },
        svg("title", {}, label),
        svg("line", { x1: 10, x2: 170, y1: 15, y2: 15, class: "strip-track" }),
        svg("circle", { cx: x(a), cy: 15, r: 5, class: "sample-a" }),
        svg("path", { d: `M${x(b)},9 l6,6 -6,6 -6,-6 z`, class: "sample-b" })),
      dataTable(`${metric.label}: sample A and B overlay`, ["Mark", "Value"],
        [["Sample A", rawCell(fmtMetricValue(metric, a), a, metric.unit)],
          ["Sample B", rawCell(fmtMetricValue(metric, b), b, metric.unit)],
          ["B−A", rawCell(fmtMetricValue(metric, metric.delta_b_minus_a, { role: "difference" }),
            metric.delta_b_minus_a, metric.unit)]]),
      h("figcaption", {}, "A ● · B ◆; local display scale"));
  }

  function sampleCompare(sample) {
    const section = h("section", { class: "panel", "aria-labelledby": "compare-sample-title" },
      h("h2", { id: "compare-sample-title" }, "Another sample"),
      h("p", { class: "help" }, "Cross-sectional comparison of two distinct samples. Source IDs do not establish a shared donor or timepoint. Only compatible current full-input results are paired; B−A is an arithmetic difference in the displayed unit, not a clinical threshold."));
    const others = (L.state.samples || []).filter((row) => row.id12 !== sample.id12 && !row.example);
    if (!others.length) {
      section.append(h("p", { class: "empty" }, "No other real sample is available under this ROOT."));
      return section;
    }
    const picker = h("select", { id: "compare-other-sample", "data-requires-server": "" },
      ...others.map((row) => h("option", { value: row.id12 }, row.id12)));
    const result = h("div", { "aria-live": "polite" });
    section.append(h("label", { for: "compare-other-sample" }, "Sample B "), picker, result);
    let request = 0;
    const show = () => {
      const current = ++request;
      result.replaceChildren(h("p", {}, "Loading sample comparison…"));
      fetch(`/api/samples/${sample.id12}/compare/${encodeURIComponent(picker.value)}`, { credentials: "same-origin" })
        .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
        .then((data) => {
          if (!result.isConnected || current !== request) return;
          if (data.state !== "ready") {
            const messages = {
              same_sample: "Choose a different sample.",
              sample_unavailable: `Sample ${data.which === "b" ? "B" : "A"} is unavailable; reload the sample list after checking its current full input.`,
              stale_sample: `Sample ${data.which === "b" ? "B" : "A"} changed while Compare loaded; reload before comparing.`,
              example_unscorable: "Synthetic EXAMPLE samples are not paired with real kit samples.",
            };
            result.replaceChildren(h("p", { class: "problem" }, messages[data.state] || "The selected comparison is unavailable."));
            return;
          }
          const nodes = [h("p", {}, `Sample A ${data.id12_a} · Sample B ${data.id12_b}. A: ${data.aligner_a}; B: ${data.aligner_b}. Only equal compatibility keys and exact selected results appear below.`)];
          if (data.metrics.length) {
            const rows = data.metrics.map((metric) => h("tr", {},
              h("th", { scope: "row" }, metric.algorithm_id, " · ", metric.label),
              h("td", {}, sampleOverlay(metric)),
              h("td", { class: "num" }, fmtMetricValue(metric, metric.value_a)),
              h("td", { class: "num" }, fmtMetricValue(metric, metric.value_b)),
              h("td", { class: "num" }, fmtMetricValue(metric, metric.delta_b_minus_a, { role: "difference" }))));
            nodes.push(h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Sample A and B comparison" },
              h("table", { class: "data-table" },
                h("caption", {}, `Sample A ${data.id12_a} versus B ${data.id12_b}; displayed values and B−A`),
                h("thead", {}, h("tr", {}, ...["Metric", "Overlay", "Sample A", "Sample B", "B−A"].map((name) => h("th", { scope: "col" }, name)))),
                h("tbody", {}, ...rows))));
          } else nodes.push(h("p", { class: "empty" }, "No compatible scalar metric is available for this pair. Missing values are not zero."));
          if (data.unscored.some((row) => row.reason === "not_current_result")) nodes.push(h("p", { class: "help" },
            "A stored result is no longer current for its registered method. Recompute with the current method, or restore its registration before comparing."));
          if (data.unscored.length) nodes.push(h("details", {}, h("summary", {}, `Not paired (${data.unscored.length})`),
            dataTable("Unpaired metrics and reasons", ["Algorithm", "Metric", "Reason"],
              data.unscored.map((row) => [row.algorithm_id, row.metric_id, row.reason.replace(/_/g, " ")]))));
          result.replaceChildren(...nodes);
        })
        .catch((error) => {
          if (result.isConnected && current === request) result.replaceChildren(h("p", { class: "problem" }, `Sample comparison could not be loaded (${error.message}). Reload Compare.`));
        });
    };
    picker.addEventListener("change", show);
    show();
    return section;
  }

  window.TracebackLabViews = {
    signals,
    compare,
    "body-map": bodyMap,
    agreement,
    landing,
    landingCliaPanel,
    findings,
    stability,
    evidence,
    methods,
    clia: () => { const holder = h("div", {}, h("h1", { class: "view-title" }, "What CLIA means for this demo"), h("p", {}, "Loading…")); clia().then((nodes) => { holder.replaceChildren(...nodes); const t = holder.querySelector("h1"); if (t) { t.tabIndex = -1; t.focus(); } }); return [holder]; },
  };
})();
