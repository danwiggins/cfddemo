// Traceback Lab: the all-methods overview and the per-method result page.
// Same rules as lab.js: same-origin script, no innerHTML, no inline styles;
// every chart has an aria-label and an adjacent data table.
// Research use only. Not CLIA-validated. Not a diagnosis.
"use strict";

(function () {
  const L = window.TracebackLab;
  const V = window.TracebackLabViews;
  if (!L || !V || !V.parts) return;
  const { h, fmt, fmtMetric, fmtCount, rawCell, dataTable, chart } = L;
  const P = V.parts;

  // Groups describe what a method measures. They never order or group by a
  // measured value; members keep this fixed order.
  const GROUPS = [
    { id: "quality", label: "Quality and identity",
      blurb: "Is the input good enough to analyse, and does it behave like the sample it claims to be?",
      ids: ["bam-qc", "depth", "long-tail", "modbase-qc", "sex-check", "identity"] },
    { id: "fragment-size", label: "Fragment size",
      blurb: "How long are the DNA fragments, and how does that vary across the genome and the mitochondria?",
      ids: ["fragment-size", "short-read-fragment-size", "mtdna", "delfi-ratio"] },
    { id: "fragment-ends", label: "Fragment ends and DNA packaging",
      blurb: "Where fragments start and stop, and what that suggests about how the DNA was packaged.",
      ids: ["end-motifs", "breakpoint-motifs", "aligned-end-structure", "finaletoolkit-xcheck", "wps",
        "tss-profile", "griffin", "lionheart"] },
    { id: "copy-number", label: "Copy number",
      blurb: "Are some chromosome regions over- or under-represented compared with the rest?",
      ids: ["copy-number", "instability", "arm-summary"] },
    { id: "methylation", label: "Methylation",
      blurb: "How much of the DNA carries methyl marks, genome-wide and molecule by molecule?",
      ids: ["global-5mc", "global-5hmc", "pmd-score", "hypometh-reads", "frag-x-meth", "epi-clock"] },
    { id: "origin", label: "Tissue and cell origin",
      blurb: "Which reference cell types or mixture components best describe the observed pattern?",
      ids: ["cell-origin", "meth-tf", "methylbert"] },
    { id: "fractions", label: "Fetal and donor fractions",
      blurb: "Exploratory estimates of the share of DNA from a second genome, when the scenario applies.",
      ids: ["fetal-fraction", "fetal-fraction-chry", "dd-cfdna-chry"] },
    { id: "non-human", label: "Non-human DNA",
      blurb: "Which microbial or viral sequences appear among the reads that do not map to the human reference?",
      ids: ["microbial", "viral-reads"] },
  ];
  const NAMES = {
    "bam-qc": "Alignment quality", depth: "Sequencing depth", "long-tail": "Long fragments",
    "modbase-qc": "Modification-call quality", "sex-check": "Chromosomal sex check",
    identity: "Sample identity fingerprint", "fragment-size": "Fragment length",
    "short-read-fragment-size": "Short-read fragment length", mtdna: "Mitochondrial DNA share",
    "delfi-ratio": "Short/long ratio across the genome", "end-motifs": "Fragment end motifs",
    "breakpoint-motifs": "Fragment ends near CpG", "aligned-end-structure": "Open-chromatin fragment ends",
    "finaletoolkit-xcheck": "FinaleToolkit cross-check", wps: "Windowed protection score",
    "tss-profile": "Coverage around gene starts", griffin: "Griffin TF-site coverage",
    lionheart: "LIONHEART accessibility scores", "copy-number": "Copy-number profile",
    instability: "Copy-number instability", "arm-summary": "Chromosome-arm summary",
    "global-5mc": "Genome-wide 5mC", "global-5hmc": "Genome-wide 5hmC",
    "pmd-score": "Partially methylated domains", "hypometh-reads": "Hypomethylated reads",
    "frag-x-meth": "Fragment length × methylation", "epi-clock": "Epigenetic age clock",
    "cell-origin": "Cell-type origin", "meth-tf": "Methylation mixture fit",
    methylbert: "MethylBERT mixture", "fetal-fraction": "Fetal fraction",
    "fetal-fraction-chry": "Fetal fraction from chrY", "dd-cfdna-chry": "Donor-derived DNA from chrY",
    microbial: "Microbial reads", "viral-reads": "Viral reads",
  };
  // Plain-English reasons the shared readiness table does not cover.
  const EXTRA_REASONS = {
    derived_not_supported: ["Full input only", "This method runs on the full input, not on downsampled or split controls."],
    derived_input: ["Full input only", "This method runs on the full input, not on downsampled or split controls."],
    qc: ["Input quality gate", "Review Trust/QC for the quality check that stopped this method."],
    below_lod: ["Below detection limit", "The signal is below the method's documented limit at this depth."],
    input_unavailable: ["Input unavailable", "Restore the recorded input or rerun the sample."],
    protocol_mismatch: ["Protocol mismatch", "Use an input prepared with the protocol this method requires."],
  };
  const STATUS = {
    ok: { glyph: "●", label: "Ran" },
    underpowered: { glyph: "▨", label: "Underpowered · not a call", short: "underpowered (not a call)" },
    skipped: { glyph: "○", label: "Skipped" },
    failed: { glyph: "✕", label: "Failed" },
    "not-run": { glyph: "–", label: "Not run" },
    unavailable: { glyph: "?", label: "Result unavailable" },
  };
  const FILTERS = ["ok", "underpowered", "skipped", "failed", "not-run", "unavailable"];
  const name = (id) => NAMES[id] || id;
  const algo = (sample, id) => (sample.algorithms || []).find((a) => a.algorithm_id === id) || null;
  const humanize = (text) => String(text || "").replace(/_/g, " ");

  function reasonText(reason) {
    const row = P.readinessReason(reason) || EXTRA_REASONS[reason];
    return row ? { title: row[0], fix: row[1], anchor: row[2] || null }
      : { title: reason ? humanize(reason) : "No reason recorded", fix: "Check the run log for this method.", anchor: null };
  }

  function statusOf(sample, id) {
    const a = algo(sample, id);
    if ((sample.unreadable_results || []).includes(id)) return { key: "unavailable", detail: "Selected result unreadable" };
    if ((sample.ambiguous_results || []).includes(id)) return { key: "unavailable", detail: "Result selection ambiguous" };
    if (!a) return { key: "not-run", detail: "No result recorded for this sample" };
    if (a.status === "ok" && a.underpowered) return { key: "underpowered", detail: "Ran below its depth floor; exploratory only" };
    if (a.status === "ok") return { key: "ok", detail: null };
    if (a.status === "skipped") return { key: "skipped", detail: reasonText(a.skip_reason).title };
    if (a.status === "failed") return { key: "failed", detail: a.failure?.reason === "memory" ? "Stopped by the memory cap" : "See the failure detail" };
    return { key: "unavailable", detail: humanize(a.status) };
  }

  function statusPill(key, extra) {
    const s = STATUS[key] || STATUS.unavailable;
    return h("span", { class: `status-pill status-${key}` },
      h("span", { class: "status-glyph", "aria-hidden": "true" }, s.glyph), " ", s.label, extra ? ` · ${extra}` : "");
  }

  function groupedCatalog(sample) {
    const catalog = sample.catalog || [];
    const byId = new Map(catalog.map((row) => [row.algorithm_id, row]));
    const placed = new Set();
    const groups = GROUPS.map((group) => {
      const rows = group.ids.filter((id) => byId.has(id)).map((id) => byId.get(id));
      rows.forEach((row) => placed.add(row.algorithm_id));
      return { ...group, rows };
    });
    const rest = catalog.filter((row) => !placed.has(row.algorithm_id));
    if (rest.length) groups.push({ id: "other", label: "Other methods", blurb: "Methods not yet assigned to a group.", rows: rest });
    return groups.filter((group) => group.rows.length);
  }

  function orderedIds(sample) {
    return groupedCatalog(sample).flatMap((group) => group.rows.map((row) => row.algorithm_id));
  }

  function headlineMetric(a) {
    if (!a || !a.headline_metric) return null;
    return (a.metrics || []).find((m) => m.metric_id === a.headline_metric) || null;
  }

  function resultHref(sample, id) { return `#/sample/${sample.id12}/result?algorithm=${id}`; }

  // -- inputs: the full input plus its downsampled and split controls -------------------------
  function inputs(sample) {
    const derived = sample.derived || [];
    return [{ label: "full", text: "Full input", transform: "full" },
      ...derived.filter((row) => row.transform === "downsample")
        .sort((a, b) => Number(b.label) - Number(a.label))
        .map((row) => ({ label: row.label, text: `${row.label}× downsample`, transform: "downsample" })),
      ...derived.filter((row) => row.transform === "split")
        .map((row) => ({ label: row.label, text: `Split half ${row.label.replace("split-", "")}`, transform: "split" }))];
  }
  function selectedInput(sample) {
    const remembered = L.state.labuiInput;
    const label = remembered && remembered.id12 === sample.id12 ? remembered.label : "full";
    return inputs(sample).some((row) => row.label === label) ? label : "full";
  }
  function setSelectedInput(sample, label) { L.state.labuiInput = { id12: sample.id12, label }; }

  // The headline value of one method on one derived input, from the depth curve or split record.
  function derivedCell(sample, id, label) {
    if (label.startsWith("split-")) {
      const panel = (sample.stability || []).find((row) => row.algorithm_id === id && row.headline !== false);
      const half = panel?.split_status?.[label.replace("split-", "")];
      return half ? { state: half.state === "ok" ? "ok_usable" : half.state, value: half.value, reason: half.reason } : null;
    }
    const row = (sample.curve?.rows || []).find((r) => r.algorithm_id === id);
    return row ? (row.cells || []).find((cell) => cell.label === label) || null : null;
  }

  function inputPicker(sample, onChange) {
    const current = selectedInput(sample);
    const group = h("div", { class: "input-picker", role: "group", "aria-label": "Input shown" });
    for (const row of inputs(sample)) {
      group.append(h("button", { type: "button", class: "chip-button", "aria-pressed": String(row.label === current),
        "data-input": row.label, onclick: () => {
          setSelectedInput(sample, row.label);
          for (const button of group.querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.input === row.label));
          onChange(row.label);
        } }, row.text));
    }
    return group;
  }

  // -- overview -------------------------------------------------------------------------------
  function tile(sample, row, label) {
    const id = row.algorithm_id;
    const a = algo(sample, id);
    const status = statusOf(sample, id);
    const body = [];
    if (label === "full") {
      if (status.key === "ok") {
        const metric = headlineMetric(a);
        if (metric) {
          const [glyph, text] = P.STATE[metric.state] || ["", humanize(metric.state)];
          body.push(h("span", { class: "tile-value" }, fmtMetric(metric, { algorithmId: id })),
            h("span", { class: "tile-state" }, h("span", { "aria-hidden": "true" }, `${glyph} `), text));
        } else body.push(h("span", { class: "tile-state" }, "No headline metric"));
      } else if (status.detail) body.push(h("span", { class: "tile-state" }, status.detail));
    } else {
      const cell = derivedCell(sample, id, label);
      const metric = headlineMetric(a);
      if (!cell) body.push(h("span", { class: "tile-state" }, "Not recorded for this input"));
      else {
        if (metric && cell.value !== null && cell.value !== undefined && cell.state === "ok_usable") {
          body.push(h("span", { class: "tile-value" }, L.fmtMetricValue(metric, cell.value, { algorithmId: id })));
        }
        body.push(h("span", { class: "tile-state" }, P.cellText(cell.state, cell.reason ? humanize(cell.reason) : null)));
      }
    }
    const key = label === "full" ? status.key : "derived";
    return h("li", { class: "ov-item", "data-status": status.key, "data-algorithm-id": id },
      h("a", { class: `ov-tile tile-${key}`, href: resultHref(sample, id) },
        h("span", { class: "tile-head" }, statusPill(status.key), row.tier ? P.tierBadge(row.tier) : ""),
        h("span", { class: "tile-name" }, name(id)),
        h("span", { class: "tile-measure" }, row.label),
        h("span", { class: "tile-body" }, ...body),
        h("span", { class: "tile-id mono" }, id)));
  }

  function overview(sample) {
    const groups = groupedCatalog(sample);
    const all = groups.flatMap((group) => group.rows);
    const counts = Object.fromEntries(FILTERS.map((key) => [key, 0]));
    for (const row of all) counts[statusOf(sample, row.algorithm_id).key] += 1;
    const state = { filter: "", query: "", input: selectedInput(sample) };
    const summary = h("p", { class: "ov-summary" }, `${all.length} methods · `,
      ...FILTERS.filter((key) => counts[key]).flatMap((key, index) => [index ? " · " : "",
        h("span", { class: "ov-count" }, `${counts[key]} ${STATUS[key].short || STATUS[key].label.toLowerCase()}`)]));
    const filterBar = h("div", { class: "filter-bar", role: "group", "aria-label": "Show methods by status" });
    const addFilter = (key, text) => filterBar.append(h("button", { type: "button", class: "chip-button",
      "aria-pressed": String(state.filter === key), "data-filter": key, onclick: () => {
        state.filter = key;
        for (const b of filterBar.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.filter === key));
        apply();
      } }, text));
    addFilter("", `All (${all.length})`);
    for (const key of FILTERS) if (counts[key]) addFilter(key, `${STATUS[key].glyph} ${STATUS[key].label} (${counts[key]})`);
    const search = h("input", { type: "search", id: "ov-search", class: "ov-search", autocomplete: "off",
      placeholder: "Find a method", "aria-label": "Find a method" });
    const live = h("p", { class: "visually-hidden", role: "status", "aria-live": "polite" });
    const body = h("div", { class: "ov-groups" });
    const draw = () => {
      body.replaceChildren(...groups.map((group) => {
        const ran = group.rows.filter((row) => statusOf(sample, row.algorithm_id).key === "ok").length;
        return h("section", { class: "panel ov-group", "aria-labelledby": `ov-${group.id}`, "data-group": group.id },
          h("div", { class: "ov-group-head" },
            h("h2", { id: `ov-${group.id}` }, group.label),
            h("span", { class: "ov-group-count" }, `${ran} of ${group.rows.length} ran`)),
          h("p", { class: "help" }, group.blurb),
          h("ul", { class: "ov-grid" }, ...group.rows.map((row) => tile(sample, row, state.input))));
      }));
      apply();
    };
    const apply = () => {
      const needle = state.query.trim().toLocaleLowerCase();
      let shown = 0;
      for (const item of body.querySelectorAll(".ov-item")) {
        const id = item.dataset.algorithmId;
        const row = all.find((r) => r.algorithm_id === id);
        const match = (!state.filter || item.dataset.status === state.filter) && (!needle ||
          [id, name(id), row?.label, row?.question, row?.description].some((v) => String(v || "").toLocaleLowerCase().includes(needle)));
        item.hidden = !match;
        if (match) shown += 1;
      }
      for (const section of body.querySelectorAll(".ov-group")) section.hidden = !section.querySelector(".ov-item:not([hidden])");
      live.textContent = `${shown} of ${all.length} methods shown`;
      empty.hidden = shown > 0;
    };
    const empty = h("p", { class: "empty", hidden: true }, "No method matches. Clear the search or choose All.");
    search.addEventListener("input", () => { state.query = search.value; apply(); });
    const picker = inputPicker(sample, (label) => { state.input = label; draw(); });
    const derivedNote = h("p", { class: "help ov-input-note" },
      "Downsampled and split inputs are controls made from this sample, not separate samples. On a control, each card shows the method's headline value and its recorded state on that input.");
    draw();
    return [
      h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › All methods`),
      h("div", { class: "ov-head" },
        h("h1", { class: "view-title" }, "All methods"),
        h("p", { class: "ruo-note" }, "Research use only. Not a diagnosis.")),
      summary,
      h("div", { class: "ov-controls" },
        h("div", { class: "ov-control-row" }, h("span", { class: "ov-control-label" }, "Status"), filterBar),
        (sample.derived || []).length ? h("div", { class: "ov-control-row" }, h("span", { class: "ov-control-label" }, "Input"), picker) : "",
        h("div", { class: "ov-control-row" }, h("label", { class: "ov-control-label", for: "ov-search" }, "Search"), search)),
      (sample.derived || []).length ? derivedNote : "",
      h("p", { class: "help" }, "A skipped, failed or underpowered method is not a negative finding. Underpowered results ran below the method's depth floor with ",
        h("code", {}, "--below-depth"), "; they are exploratory and never a call."),
      live, empty, body,
    ];
  }

  // Compact per-group status strip for the landing page.
  function glance(sample) {
    const groups = groupedCatalog(sample);
    const total = groups.reduce((n, group) => n + group.rows.length, 0);
    return h("section", { class: "panel ov-glance", "aria-labelledby": "ov-glance-title" },
      h("div", { class: "ov-group-head" },
        h("h2", { id: "ov-glance-title" }, "Methods at a glance"),
        h("a", { class: "ov-glance-all", href: `#/sample/${sample.id12}/overview` }, `All ${total} methods →`)),
      h("p", { class: "help" }, "● ran · ▨ underpowered (not a call) · ○ skipped · ✕ failed · – not run · ? unavailable"),
      h("ul", { class: "glance-rows" }, ...groups.map((group) => h("li", { class: "glance-row" },
        h("span", { class: "glance-label" }, group.label),
        h("span", { class: "glance-dots" }, ...group.rows.map((row) => {
          const status = statusOf(sample, row.algorithm_id);
          return h("a", { class: `glance-dot status-${status.key}`, href: resultHref(sample, row.algorithm_id),
            "aria-label": `${name(row.algorithm_id)}: ${STATUS[status.key].label}${status.detail ? ` (${status.detail})` : ""}`,
            title: `${name(row.algorithm_id)}: ${STATUS[status.key].label}` }, STATUS[status.key].glyph);
        }))))));
  }

  // -- per-method result page -------------------------------------------------------------------
  function statusSection(sample, id, a, status) {
    const section = h("section", { class: `panel result-status status-panel-${status.key}`, "aria-labelledby": "result-status-title" },
      h("h2", { id: "result-status-title" }, "Status"), h("p", {}, statusPill(status.key)));
    if (status.key === "ok") {
      section.append(h("p", {}, `Ran on the full input${a.duration_s != null ? ` in ${fmt(a.duration_s, "s")}` : ""}. `,
        "Values below are measurements with their interpretation state; they are not a diagnosis."));
    } else if (status.key === "underpowered") {
      section.append(P.underpoweredBanner(a),
        h("p", {}, "This method ran because the run used ", h("code", {}, "--below-depth"),
          ". Its metrics are marked not interpretable (depth). They never feed cohorts, stability verdicts or spike-recovery verdicts."),
        h("p", {}, h("strong", {}, "To get a usable result: "), "sequence deeper or pool more input until the depth floor is met, then rerun without ",
          h("code", {}, "--below-depth"), "."));
    } else if (status.key === "skipped") {
      const reason = reasonText(a.skip_reason);
      section.append(
        h("p", {}, h("strong", {}, "Why: "), reason.title, a.skip_message ? ` — ${a.skip_message}.` : "."),
        h("p", {}, h("strong", {}, "How to fix: "), reason.fix,
          reason.anchor ? [" ", h("a", { href: `${P.readinessGuide}${reason.anchor}`, target: "_blank", rel: "noreferrer noopener" }, "Lab guide (external)")] : ""),
        h("p", { class: "help" }, "A skipped method is not a negative finding."));
    } else if (status.key === "failed") {
      const failure = a.failure || {};
      section.append(
        h("p", {}, h("strong", {}, "What happened: "), failure.first_line || "No failure line recorded.",
          failure.timed_out ? " (timed out)" : "", failure.reason === "memory" ? " (stopped by the per-method memory cap)" : "",
          failure.exit_code != null ? ` · exit code ${failure.exit_code}` : ""),
        h("p", {}, h("strong", {}, "How to fix: "), failure.reason === "memory"
          ? "Raise the memory cap or run this method alone, then rerun." : "Read the error tail below and the run log in Trust/QC, fix the cause, then rerun.", " ",
        h("a", { href: `#/sample/${sample.id12}/trust-qc` }, "Trust/QC →")),
        (failure.stderr_tail || []).length ? h("details", {}, h("summary", {}, "Error tail"),
          h("pre", { class: "stderr" }, failure.stderr_tail.join("\n"))) : "");
    } else {
      section.append(h("p", {}, status.detail || "No result is available."),
        h("p", {}, h("strong", {}, "How to fix: "), status.key === "not-run"
          ? "Rerun the sample with a Lab version that includes this method." : "Rerun the sample, or restore the result files from a known-good backup."));
    }
    return section;
  }

  function aboutSection(row) {
    const platforms = (row.platforms || []).map((v) => v === "ont" ? "ONT" : v === "illumina" ? "Illumina" : v).join(", ");
    return h("section", { class: "panel result-about", "aria-labelledby": "result-about-title" },
      h("h2", { id: "result-about-title" }, "What this method asks"),
      h("p", { class: "result-question" }, row.question || "Research question not documented."),
      h("p", {}, row.description || "Method explanation unavailable."),
      h("p", {}, h("strong", {}, "Limits: "), row.limitations || "Not documented."),
      h("details", {}, h("summary", {}, "Prerequisites"),
        h("p", {}, `Platforms: ${platforms || "unspecified"}. `,
          row.min_eligible_reads ? `Needs ${fmtCount(row.min_eligible_reads)} eligible reads. ` : "",
          row.min_depth_x ? `Literature depth minimum for the headline metric: ${row.min_depth_x}x. ` : "",
          row.requires_mm_tags ? "Needs modification (MM/ML) tags." : ""),
        (row.requirements || []).length ? h("ul", {}, ...row.requirements.map((item) => h("li", {}, item))) : ""));
  }

  function metricsSection(sample, id, a) {
    const metrics = (a.metrics || []).filter((m) => m.value === null || typeof m.value !== "object");
    const headline = headlineMetric(a);
    const section = h("section", { class: "panel result-metrics", "aria-labelledby": "result-metrics-title" },
      h("h2", { id: "result-metrics-title" }, "Result"));
    if (headline && !a.underpowered) {
      section.append(P.card(sample, { algorithm_id: id, ...headline }, new Set(), false));
    }
    if (!metrics.length) { section.append(h("p", { class: "empty" }, "No scalar metrics recorded.")); return section; }
    section.append(h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
      h("caption", {}, `${name(id)}: every recorded metric`),
      h("thead", {}, h("tr", {}, ...["Metric", "Value", "Interpretation"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ...metrics.map((m) => h("tr", { "data-metric-id": m.metric_id },
        h("th", { scope: "row" }, m.label, m.metric_id === a.headline_metric ? h("span", { class: "help" }, " (headline)") : ""),
        h("td", {}, rawCell(fmtMetric(m, { algorithmId: id }), m.value, m.unit, `${id}/${m.metric_id}`)),
        h("td", {}, P.stateChip(m))))))));
    return section;
  }

  // Draw a recorded panel from its arrays: a numeric x with numeric series, or labelled bars.
  function genericPanel(data, label) {
    if (!data || typeof data !== "object") return null;
    const arrays = Object.entries(data).filter(([, v]) => Array.isArray(v) && v.length > 1);
    const numeric = arrays.filter(([, v]) => v.every((x) => x === null || (typeof x === "number" && Number.isFinite(x))));
    const text = arrays.filter(([, v]) => v.every((x) => typeof x === "string"));
    const X_KEYS = ["span_bp", "offset_bp", "position", "pos", "bp", "x", "length_bp", "bin"];
    let xKey = X_KEYS.find((k) => numeric.some(([key]) => key === k)) || null;
    const xText = !xKey && text.length ? text[0][0] : null;
    if (!xKey && !xText && numeric.length >= 2) xKey = numeric[0][0];
    const base = xKey || xText;
    if (!base) return null;
    const n = data[base].length;
    const series = numeric.filter(([key, v]) => key !== base && v.length === n).slice(0, 4);
    if (!series.length) return null;
    let rows = data[base].map((x, i) => [x, ...series.map(([, v]) => v[i])]);
    if (xKey) {
      // Trim empty tails so a long histogram shows its body.
      let last = rows.length - 1;
      while (last > 0 && rows[last].slice(1).every((v) => !v)) last -= 1;
      let first = 0;
      while (first < last && rows[first].slice(1).every((v) => !v)) first += 1;
      rows = rows.slice(first, last + 1);
    }
    const headers = [base.replace(/_/g, " "), ...series.map(([key]) => key.replace(/_/g, " "))];
    // Series can differ in scale (a count beside its share); draw the first, tabulate all.
    const drawn = series.slice(0, 1);
    const option = xKey ? {
      grid: { left: 64, right: 20, top: 28, bottom: 44 },
      xAxis: { type: "value", name: headers[0], nameLocation: "middle", nameGap: 28, min: "dataMin", max: "dataMax" },
      yAxis: { type: "value", name: headers[1] },
      series: drawn.map(([key], i) => ({ type: "line", name: key.replace(/_/g, " "), showSymbol: false,
        data: rows.map((r) => [r[0], r[i + 1]]) })),
    } : {
      grid: { left: 64, right: 20, top: 28, bottom: 64 },
      xAxis: { type: "category", data: rows.map((r) => r[0]), axisLabel: { rotate: 60 } },
      yAxis: { type: "value", name: headers[1] },
      series: drawn.map(([key], i) => ({ type: "bar", name: key.replace(/_/g, " "), data: rows.map((r) => r[i + 1]) })),
    };
    return chart(`${label}: ${headers[1]} by ${headers[0]}.`, option, dataTable(label, headers, rows));
  }

  function chartsSection(sample, id, a, status) {
    const panels = a?.panels || [];
    if (!panels.length || !["ok", "underpowered"].includes(status.key)) return "";
    const section = h("section", { class: "panel result-charts", "aria-labelledby": "result-charts-title" },
      h("h2", { id: "result-charts-title" }, "Charts"),
      status.key === "underpowered" ? h("p", { class: "problem", role: "note" },
        "Underpowered: these charts come from a run below the method's depth floor. Exploratory only; not a call.") : "");
    const slot = h("div", { class: "result-chart-slot", "aria-live": "polite" });
    const draw = (label) => {
      const inputText = inputs(sample).find((row) => row.label === label)?.text || label;
      slot.replaceChildren(...panels.map((panel) => {
        const box = h("figure", { class: "result-chart" }, h("figcaption", {}, `${humanize(panel.panel_id)} · ${inputText}`), h("p", {}, "Loading…"));
        const revision = L.state.navigationRevision;
        const hash = location.hash;
        L.requestJson(`/api/samples/${sample.id12}/artifact/${label}/${id}/${panel.panel_id}`, {
          isCurrent: () => revision === L.state.navigationRevision && hash === location.hash && L.state.sample?.id12 === sample.id12,
        }).then((data) => {
          const drawn = genericPanel(data, `${name(id)} — ${humanize(panel.panel_id)} (${inputText})`);
          box.lastChild.replaceWith(drawn || h("p", { class: "help" }, "This panel has a dedicated view. ",
            h("a", { href: `#/sample/${sample.id12}/signals?algorithm=${id}` }, "Open it in Signals →")));
        }).catch((error) => {
          if (error.kind === "obsolete" || !box.isConnected) return;
          box.lastChild.replaceWith(h("p", { class: label === "full" ? "problem" : "empty" }, label === "full"
            ? "Chart could not be loaded. Retry loading this result; the metrics above are unaffected."
            : `No ${humanize(panel.panel_id)} panel is recorded for ${inputText}.`));
        });
        return box;
      }));
    };
    if ((sample.derived || []).length) section.append(inputPicker(sample, draw));
    section.append(slot, h("p", { class: "help" }, "Charts show recorded panel data for the selected input. ",
      h("a", { href: `#/sample/${sample.id12}/signals?algorithm=${id}` }, "Dedicated view in Signals →")));
    draw(selectedInput(sample));
    return section;
  }

  function acrossInputsSection(sample, id, a) {
    const curve = (sample.curve?.rows || []).find((r) => r.algorithm_id === id);
    const stability = (sample.stability || []).find((row) => row.algorithm_id === id && row.headline !== false);
    if (!curve && !stability) return "";
    const metric = headlineMetric(a);
    const value = (cell) => cell && cell.value !== null && cell.value !== undefined
      ? (metric ? L.fmtMetricValue(metric, cell.value, { algorithmId: id }) : fmt(cell.value)) : "—";
    const rows = [];
    for (const cell of curve?.cells || []) {
      rows.push(h("tr", {}, h("th", { scope: "row" }, cell.label === "full" ? "Full input" : `${cell.label}× downsample`),
        h("td", {}, cell.depth_x != null ? fmt(cell.depth_x, "x") : "—"),
        h("td", {}, value(cell)), h("td", {}, P.cellText(cell.state, cell.reason ? humanize(cell.reason) : null))));
    }
    for (const half of ["A", "B"]) {
      const s = stability?.split_status?.[half];
      if (!s) continue;
      rows.push(h("tr", {}, h("th", { scope: "row" }, `Split half ${half}`), h("td", {}, "—"),
        h("td", {}, value(s)), h("td", {}, s.state === "ok" ? "ran" : `${humanize(s.state)}${s.reason ? ` (${humanize(s.reason)})` : ""}`)));
    }
    return h("section", { class: "panel result-inputs", "aria-labelledby": "result-inputs-title" },
      h("h2", { id: "result-inputs-title" }, "Across inputs"),
      h("p", { class: "help" }, `Headline metric${metric ? ` (${metric.label})` : ""} on the full input and on controls made from it.`,
        stability?.verdict ? ` Stability: ${stability.verdict}.` : ""),
      rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
        h("caption", {}, `${name(id)} across inputs`),
        h("thead", {}, h("tr", {}, ...["Input", "Depth", "Value", "State"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...rows))) : h("p", { class: "empty" }, "No derived input recorded this method."),
      h("p", {}, h("a", { href: `#/sample/${sample.id12}/stability` }, "Stability view →")));
  }

  function result(sample) {
    const route = L.state.route || {};
    const id = route.context?.algorithm;
    const catalog = sample.catalog || [];
    const row = catalog.find((r) => r.algorithm_id === id);
    if (route.contextInvalid || !row) {
      return [h("p", { class: "breadcrumb" }, `Lab › ${sample.id12} › Method`),
        h("h1", { class: "view-title" }, "Method not found"),
        h("p", { class: "problem", role: "status" }, id ? `No visible method ${id} is registered for this sample.` : "This method link has no method."),
        h("p", {}, h("a", { href: `#/sample/${sample.id12}/overview` }, "All methods →"))];
    }
    const a = algo(sample, id);
    const status = statusOf(sample, id);
    const order = orderedIds(sample);
    const index = order.indexOf(id);
    const prev = index > 0 ? order[index - 1] : null;
    const next = index >= 0 && index < order.length - 1 ? order[index + 1] : null;
    const group = groupedCatalog(sample).find((g) => g.rows.some((r) => r.algorithm_id === id));
    const seen = new Set();
    const pager = h("nav", { class: "result-pager", "aria-label": "Other methods" },
      prev ? h("a", { href: resultHref(sample, prev), rel: "prev" }, `← ${name(prev)}`) : h("span", {}),
      h("a", { href: `#/sample/${sample.id12}/overview` }, "All methods"),
      next ? h("a", { href: resultHref(sample, next), rel: "next" }, `${name(next)} →`) : h("span", {}));
    const out = [
      h("nav", { class: "breadcrumb", "aria-label": "Breadcrumb" }, "Lab › ", `${sample.id12} › `,
        h("a", { href: `#/sample/${sample.id12}/overview` }, "All methods"), ` › ${group ? group.label : "Method"}`),
      h("div", { class: "result-head" },
        h("h1", { class: "view-title" }, name(id)),
        h("p", { class: "result-sub" }, row.label, " ", h("span", { class: "mono help" }, `(${id})`)),
        h("p", { class: "result-badges" }, statusPill(status.key), " ", P.tierBadge(row.tier)),
        h("p", { class: "ruo-note" }, "Research use only. Not a diagnosis.")),
      pager,
      statusSection(sample, id, a, status),
    ];
    if (a && a.status === "ok") out.push(metricsSection(sample, id, a));
    out.push(chartsSection(sample, id, a, status), acrossInputsSection(sample, id, a), aboutSection(row));
    if (a) {
      out.push(h("section", { class: "panel result-evidence", "aria-labelledby": "result-evidence-title" },
        h("h2", { id: "result-evidence-title" }, "Evidence and provenance"), ...P.context(sample, id, seen),
        h("p", {}, h("a", { href: `#/sample/${sample.id12}/methods?algorithm=${id}` }, "Method catalog entry →"), " · ",
          h("a", { href: `#/sample/${sample.id12}/evidence` }, "Evidence →"))));
    }
    out.push(pager.cloneNode(true));
    return out.filter(Boolean);
  }

  // Register: the overview is the first lens; result pages live under it.
  if (!L.LENSES.some((lens) => lens.id === "overview")) L.LENSES.unshift({ id: "overview", label: "All methods" });
  const baseLanding = V.landing;
  V.landing = (sample) => {
    const nodes = baseLanding(sample);
    const band = nodes.findIndex((node) => node instanceof Element && node.classList.contains("landing-band"));
    nodes.splice(band >= 0 ? band + 1 : nodes.length, 0, glance(sample));
    return nodes;
  };
  V.overview = overview;
  V.result = result;
  V.methodGroups = { GROUPS, NAMES, statusOf };
})();
