// Traceback Lab views: landing, Findings, Stability, #/about/clia (slice S6c).
// Same rules as lab.js: no innerHTML, no inline style attributes (CSSOM only),
// every chart has an aria-label and an adjacent data table.
"use strict";

(function () {
  const L = window.TracebackLab;
  const { h, fmt, dataTable, chart } = L;
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
    ok_unstable: ["◐", "unstable"],
    not_interpretable: ["▨", "not interpretable"],
    skipped: ["○", "skipped"],
    failed: ["✕", "failed"],
  };
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
  );

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
    for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }

  function flagPill(type, text) { return h("span", { class: "flag-pill" }, text || FLAG_TEXT[type] || `FLAG: ${type}`); }
  function tierBadge(tier) { return tier ? h("span", { class: `tier tier-${tier}` }, tier) : h("span", { class: "tier" }, "—"); }
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
  function licenseRef(sample, id) {
    const lic = (sample.licenses || []).find((x) => x.license_id === id);
    return h("a", { class: "license-pill", href: `#license-${id}` }, `${lic ? lic.headline : id} ↗`);
  }
  function licenseCallouts(sample) {
    const used = sample.licenses || [];
    if (!used.length) return h("section", { class: "panel", "aria-labelledby": "lic-title" }, h("h2", { id: "lic-title" }, "License callouts"), h("p", { class: "empty" }, "No algorithm on this page carries a license callout."));
    return h("section", { class: "panel", "aria-labelledby": "lic-title" }, h("h2", { id: "lic-title" }, "License callouts"),
      ...used.map((lic) => h("div", { class: "license-callout", id: `license-${lic.license_id}` },
        h("h3", {}, lic.headline), h("p", {}, lic.body), h("p", { class: "help" }, h("a", { href: lic.url, rel: "noreferrer noopener", target: "_blank" }, "License or source")))));
  }

  // -- landing ------------------------------------------------------------------------------
  function landing(sample) {
    const out = [h("h1", { class: "visually-hidden" }, `Traceback Lab — ${sample.id12}`)];
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
      requestAnimationFrame(() => setTimeout(measure, 0));
    });
    out.push(panel);
    if (sample.excluded_chunks && sample.excluded_chunks.length) {
      out.push(h("p", { class: "status-line" }, `Provisional: this input excludes ${sample.excluded_chunks.length} truncated chunks; depth reflects the reads present.`));
    }
    const sentence = h("p", { class: "headline", id: "headline" }, sample.headline.sentence);
    if (sample.headline.unlock) sentence.append(" ", h("span", { title: "literature, not measured" }, sample.headline.unlock.text), h("span", { class: "visually-hidden" }, " (literature, not measured)"));
    out.push(sentence);
    // DOM order is reading order: defended, curve, controls (CSS places them in two columns).
    const band = h("div", { class: "landing-band" }, defendedBlock(sample), curveBlock(sample), controlsBlock(sample));
    out.push(band, samplesTable(), licenseCallouts(sample));
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
      list.append(h("li", { class: "defended-row" },
        h("a", { href: `#/sample/${sample.id12}/${lens}` },
          h("span", { class: "defended-label" }, row.label), " ",
          h("strong", {}, fmt(row.value, row.unit)), " ",
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
      h("span", { class: "legend", "aria-hidden": "true" }, "● usable ◐ unstable ▨ not interpretable ○ skipped ✕ failed ◇ literature"));
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
      const name = h("button", { type: "button", class: "curve-label", "aria-expanded": "false" }, row.label);
      const cells = h("div", { class: "curve-track" });
      for (const cell of row.cells) {
        const [glyph, text] = CELL[cell.state] || ["·", "no result"];
        const mark = h("span", { class: `curve-cell cell-${cell.state || "none"}`, title: `${cell.label}: ${text}` }, h("span", { "aria-hidden": "true" }, glyph), h("span", { class: "visually-hidden" }, `${cell.label} ${text}`));
        if (cell.depth_x) mark.style.left = pos(cell.depth_x);
        cells.append(mark);
      }
      if (row.min_depth_x && (!run || row.min_depth_x > run)) {
        const lit = h("span", { class: "curve-lit", title: "literature, not measured" }, row.min_depth_x > 10 ? "≥10x →" : "◇ lit.");
        lit.style.left = pos(Math.min(row.min_depth_x, 10));
        cells.append(lit);
      }
      const detail = h("div", { class: "curve-detail", hidden: true }, `${row.label}: full-depth state ${row.full_state}; tier ${row.tier}; literature minimum ${row.min_depth_x ? row.min_depth_x + "x" : "none"}.`);
      name.addEventListener("click", () => { const open = name.getAttribute("aria-expanded") !== "true"; name.setAttribute("aria-expanded", String(open)); detail.hidden = !open; });
      line.append(name, cells);
      grid.append(line, detail);
    }
    section.append(grid);
    section.append(dataTable("Depth curve cells (literature minimums are not measured)", ["Algorithm", ...curve.fractions.map((f) => `${f * 100}%`), "Literature minimum"],
      curve.rows.map((row) => [row.label, ...row.cells.map((c) => (CELL[c.state] || ["", "—"])[1]), row.min_depth_x ? `${row.min_depth_x}x (literature, not measured)` : "—"])));
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
      const points = rows.map((r) => [r.tf_expected, r.tf_recovered === null ? null : r.tf_recovered]);
      const recovered = rows.filter((r) => r.recovered).map((r) => r.tf_expected);
      const missed = rows.filter((r) => !r.recovered && r.tf_expected >= 0.1).map((r) => r.tf_expected);
      const caption = recovered.length && !missed.length
        ? `Recovered within ±0.05 at ${recovered.join(" and ")}`
        : `Recovered within ±0.05 at ${recovered.length ? recovered.join(" and ") : "none"}; not recovered at ${missed.join(" and ") || "—"} (this depth: ${sample.depth ? sample.depth.approx_depth_x.toPrecision(3) : "?"}x)`;
      syn.append(chart(`Synthetic CNA spike: expected against recovered tumour fraction. ${caption}.`, {
        grid: { left: 50, right: 16, top: 16, bottom: 40 },
        xAxis: { type: "value", name: "expected TF", min: 0, max: 0.35 },
        yAxis: { type: "value", name: "recovered TF", min: 0, max: 0.35 },
        series: [
          { type: "line", data: [[0, 0.05], [0.3, 0.35]], lineStyle: { type: "dashed", color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "line", data: [[0.05, 0], [0.35, 0.3]], lineStyle: { type: "dashed", color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "line", data: [[0, 0], [0.35, 0.35]], lineStyle: { color: "#4b5563" }, showSymbol: false, silent: true },
          { type: "scatter", data: points.filter((p) => p[1] !== null), symbolSize: 10, itemStyle: { color: "#1f5f8b" } },
        ],
      }, dataTable("Synthetic CNA spike", ["Expected TF", "Recovered TF", "Recovered (±0.05)", "Status"], rows.map((r) => [r.tf_expected, r.tf_recovered, r.recovered, r.status]))), h("p", { class: "help" }, caption));
    }
    // REPEATABILITY
    const rep = h("div", { class: "control-block" }, h("span", { class: "control-badge" }, "REPEATABILITY"), " sampling noise, not a known answer");
    if (sample.repeatability) {
      const r = sample.repeatability;
      rep.append(h("p", {}, `Median split-half |A−B|: ${(r.median_relative_abs_diff * 100).toFixed(1)}% across ${r.metrics} metrics; ${r.unstable} outside their stability tolerance.`),
        h("ul", {}, ...r.worst.map((w) => h("li", {}, `${w.label}: ${(w.relative_abs_diff * 100).toFixed(1)}%${w.stable === false ? " (unstable)" : ""}`))),
        h("a", { href: `#/sample/${sample.id12}/stability` }, "→ Stability"));
    } else {
      rep.append(h("p", { class: "empty" }, `Run traceback lab run ${sample.id12}.`));
    }
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
        h("th", { scope: "row" }, h("a", { href: `#/sample/${row.id12}/findings` }, row.id12), row.example ? h("span", { class: "flag-pill example-pill" }, " EXAMPLE (synthetic)") : ""),
        h("td", {}, row.run_utc ? row.run_utc.slice(0, 10) : "—"),
        h("td", { class: "num" }, row.depth_x !== null ? `${row.depth_x.toPrecision(3)}x` : "—"),
        h("td", {}, `${row.counts.ok} / ${row.counts.skipped} / ${row.counts.failed}`),
        h("td", { class: "num" }, String(row.outside_reference)),
        h("td", {}, row.controls.spike === null ? "spike not run" : row.controls.spike ? "spike recovered ✓" : "spike not recovered"),
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
        svg("text", { x: x(v), y: 22, class: "strip-mark", "text-anchor": "middle" }, "◆")), h("figcaption", {}, metric.caption));
    }
    if (metric.exploratory_n) return h("figure", { class: "strip" }, h("figcaption", {}, metric.caption));
    if (metric.percentile !== null && metric.percentile !== undefined) {
      const x = (p) => 10 + (p / 100) * (width - 20);
      return h("figure", { class: "strip" }, svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": metric.caption, class: "strip-svg" },
        svg("title", {}, metric.caption),
        svg("rect", { x: x(5), y: 10, width: x(95) - x(5), height: 14, class: "strip-band" }),
        ...[1, 5, 95, 99].map((p) => svg("line", { x1: x(p), x2: x(p), y1: 6, y2: 28, class: "strip-tick" })),
        svg("text", { x: x(metric.percentile), y: 22, class: "strip-mark", "text-anchor": "middle" }, "◆")), h("figcaption", {}, metric.caption));
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
      h("p", {}, h("strong", {}, "Next steps (analytical): "), (steps.analytical || []).join("; ") || "—"),
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
      h("p", { class: "card-value" }, h("strong", {}, fmt(metric.value, metric.unit))),
      strip(metric),
      h("p", {}, "Performance: ", perf ? `${perf.value} (${perf.platform || ""}${perf.depth ? ", " + perf.depth : ""}; ${perf.source || ""})` : (entry.performance_missing_reason || "none published"),
        entry.citation_checked ? "" : [" ", h("span", { class: "unverified" }, "UNVERIFIED citation")]),
      meaning ? h("p", {}, meaning.split(". ")[0] + ".") : "",
      h("p", {}, ...(entry.flags || []).map((f) => flagPill(f)), ...licenses),
      toggle, details);
  }

  function provenance(sample, algorithmId) {
    const a = sample.algorithms.find((x) => x.algorithm_id === algorithmId);
    const p = a ? a.provenance : {};
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
    const out = [
      h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Findings`),
      h("div", { class: "findings-head" }, h("h1", { class: "view-title" }, "Findings"), flagPill("CLIA")),
      h("p", { class: "findings-summary" }, `${outside} outside a reference · ${groups.not_interpretable.length} not interpretable · ${groups.no_reference.length} awaiting a reference (cohort n = ${sample.cohort_n} of 10) · ${groups.in_range.length} in range`),
    ];
    if (sample.excluded_chunks && sample.excluded_chunks.length) out.push(h("p", { class: "status-line" }, `Provisional: this input excludes ${sample.excluded_chunks.length} truncated chunks; depth reflects the reads present.`));
    for (const a of sample.algorithms.filter((x) => x.status === "failed")) {
      out.push(h("div", { class: "problem" }, `${a.algorithm_id} failed: ${a.failure ? a.failure.first_line : ""}. `, h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Details in Trust/QC →")));
    }
    const seen = new Set();
    let first = true;
    if (!outside) {
      const withReference = metrics.filter((m) => m.state !== "no_reference" && m.state !== "not_interpretable").length;
      out.push(h("p", { class: "empty" }, `No metric is outside a reference. ${withReference} metrics have a reference; ${groups.no_reference.length} await the cohort (n = ${sample.cohort_n} of 10). `,
        h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Trust/QC"), " · ", h("a", { href: `#/sample/${sample.id12}/stability` }, "Stability")));
    }
    const titles = { markedly_above: "Markedly above", markedly_below: "Markedly below", above_typical: "Above typical", below_typical: "Below typical" };
    for (const key of Object.keys(titles)) {
      if (!groups[key].length) continue;
      out.push(h("h2", {}, `${titles[key]} (${groups[key].length})`));
      for (const m of groups[key]) { out.push(card(sample, m, seen, first)); first = false; }
    }
    const reasons = (list) => { const c = {}; for (const m of list) c[m.reason || "—"] = (c[m.reason || "—"] || 0) + 1; return Object.entries(c).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · "); };
    out.push(collapsed(`Not interpretable (${groups.not_interpretable.length})`, reasons(groups.not_interpretable), ["Metric", "Value", "Reason", "Needs"],
      groups.not_interpretable.map((m) => [m.label, fmt(m.value, m.unit), (m.reason || "").replace(/_/g, " "), m.entry && m.entry.min_depth_x ? `${m.entry.min_depth_x}x` : "—"])));
    out.push(collapsed(`No reference (${groups.no_reference.length})`, reasons(groups.no_reference), ["Metric", "Value", "Reason"],
      groups.no_reference.map((m) => [m.label, fmt(m.value, m.unit), (m.reason || "").replace(/_/g, " ")])));
    out.push(collapsed(`In range (${groups.in_range.length})`, "", ["Metric", "Value", "Reference"],
      groups.in_range.map((m) => [m.label, fmt(m.value, m.unit), m.caption])));
    const expand = h("button", { type: "button", class: "control" }, "Expand all");
    expand.addEventListener("click", () => { for (const b of document.querySelectorAll(".finding-card button[aria-expanded]")) { b.setAttribute("aria-expanded", "true"); document.getElementById(b.getAttribute("aria-controls")).hidden = false; } });
    out.splice(3, 0, expand);
    return out;
  }

  function collapsed(title, breakdown, headers, rows) {
    return h("details", { class: "finding-group" }, h("summary", {}, title, breakdown ? h("span", { class: "help" }, `  ${breakdown}`) : ""),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, String(c)))))))));
  }

  // -- Stability ------------------------------------------------------------------------------
  function stability(sample) {
    const out = [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Stability`), h("h1", { class: "view-title" }, "Stability")];
    if (!sample.curve.downsampled) out.push(h("p", { class: "empty" }, `This run used --no-downsample. Run traceback lab run ${sample.id12} to draw the curve.`));
    if (!sample.repeatability) out.push(h("p", { class: "help" }, `Split-half markers absent: run traceback lab run ${sample.id12}.`));
    const grid = h("div", { class: "multiples" });
    const tableRows = [];
    for (const panel of sample.stability) {
      const w = 240, hgt = 140, pad = 28;
      const fracs = [0.1, 0.25, 0.5, 1.0];
      const x = (f) => pad + ((Math.log10(f) - Math.log10(0.08)) / (Math.log10(1.2) - Math.log10(0.08))) * (w - pad - 8);
      const unit = (panel.points.find((p) => p.unit) || {}).unit || "percent";
      const clip = 50;
      const maxAbs = unit === "unit" ? Math.max(1e-9, ...panel.points.map((p) => Math.abs(p.deviation || 0)), ...(panel.points.map((p) => p.band ? Math.max(Math.abs(p.band.lo), Math.abs(p.band.hi)) : 0).filter(Number.isFinite))) : clip;
      const y = (v) => { const c = Math.max(-maxAbs, Math.min(maxAbs, v)); return hgt / 2 - (c / maxAbs) * (hgt / 2 - 12); };
      const marks = [svg("line", { x1: pad, x2: w - 8, y1: y(0), y2: y(0), class: "zero" })];
      for (const p of panel.points) {
        if (p.band && Number.isFinite(p.band.lo) && Number.isFinite(p.band.hi)) marks.push(svg("rect", { x: x(p.fraction) - 6, width: 12, y: y(p.band.hi), height: Math.max(1, y(p.band.lo) - y(p.band.hi)), class: "band" }));
        if (p.state === "ok_usable" || p.state === "ok_unstable") {
          const clipped = unit !== "unit" && Math.abs(p.deviation) > clip;
          marks.push(svg("text", { x: x(p.fraction), y: y(p.deviation) + 4, "text-anchor": "middle", class: "pt" }, clipped ? (p.deviation > 0 ? "↑" : "↓") : "●"));
        } else {
          marks.push(svg("text", { x: x(p.fraction), y: hgt - 6, "text-anchor": "middle", class: "gap" }, (p.reason || p.state || "gap").toString().slice(0, 10)));
        }
      }
      if (panel.split) {
        marks.push(svg("text", { x: x(0.5) - 9, y: y(panel.split.deviation_a) + 4, "text-anchor": "middle", class: "split" }, "○"));
        marks.push(svg("text", { x: x(0.5) + 9, y: y(panel.split.deviation_b) + 4, "text-anchor": "middle", class: "split" }, "○"));
      }
      for (const f of fracs) marks.push(svg("text", { x: x(f), y: hgt - 18, "text-anchor": "middle", class: "axis" }, `${f * 100}%`));
      const label = `${panel.label}: ${panel.verdict}. Deviation from full depth in ${unit === "unit" ? "the metric's unit" : "%"}${panel.split ? `; split halves ${panel.split.stable ? "stable" : "unstable"}` : ""}.`;
      grid.append(h("figure", { class: "multiple" }, h("figcaption", {}, h("strong", {}, panel.label), ` — ${panel.verdict}`),
        svg("svg", { viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label, class: "multiple-svg" }, svg("title", {}, label), ...marks),
        h("p", { class: "help" }, unit === "unit" ? "y: difference from full depth (metric's unit)" : "y: relative deviation from full depth, % (clipped ±50%)")));
      for (const p of panel.points) tableRows.push([panel.label, `${p.fraction * 100}%`, p.value, p.deviation, p.state || "—"]);
      if (panel.split) tableRows.push([panel.label, "split A / B", `${fmt(panel.split.a)} / ${fmt(panel.split.b)}`, `${(panel.split.relative_abs_diff * 100).toFixed(1)}% |A−B|`, panel.split.stable ? "stable" : "unstable"]);
    }
    out.push(grid, dataTable("Stability: every point", ["Metric", "Fraction", "Value", "Deviation", "State"], tableRows));
    return out;
  }

  async function clia() {
    const c = await cliaCopy();
    return [h("p", { class: "back" }, h("a", { href: "#/" }, "← Back to Lab home")), h("h1", { class: "view-title" }, c.title), ...paragraphs(c.full), h("p", { class: "help" }, `Last reviewed: ${c.last_reviewed}`)];
  }

  window.TracebackLabViews = {
    landing,
    findings,
    stability,
    clia: () => { const holder = h("div", {}, h("h1", { class: "view-title" }, "What CLIA means for this demo"), h("p", {}, "Loading…")); clia().then((nodes) => { holder.replaceChildren(...nodes); const t = holder.querySelector("h1"); if (t) { t.tabIndex = -1; t.focus(); } }); return [holder]; },
  };
})();
