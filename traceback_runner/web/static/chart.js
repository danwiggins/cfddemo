// Fragment-length histogram for one local record (usability C1).
//
// Bars have variable width: each spans its bin's edges on a linear bp axis,
// and its height is the share of eligible alignments per bp, so a bar's AREA
// is its share and wide bins are not over-weighted.  The open final bin
// (for example 1000+) has no width; it is drawn hatched from its lower edge to
// lower edge + 200 bp with height share / 200, as a display convention that a
// visible footnote and its table row state.  No reference band, threshold or
// colour judgement is drawn.  The exact counts are in the table that follows
// the chart, which is the authoritative view.
//
// Policies with more than 50 bins (1-bp presets) draw one line over per-bp
// densities, carry no per-bar labels, and group the table into 10-bp rows.
//
// Pure DOM construction: createElement / createElementNS / setAttribute /
// textContent only, no style attributes (the server's CSP forbids them).
(() => {
  "use strict";
  // Built from parts: packaged assets may not contain a URL literal.
  const SVG_NS = ["http", "://www.w3.org/2000/svg"].join("");
  const OPEN_DRAW_BP = 200;
  const MANY_BINS = 50;
  const GROUP_BP = 10;

  // The element factories (same shape as longitudinal.js's svg()/el()).
  const svg = (doc, tag, attrs) => {
    const node = doc.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([name, value]) => node.setAttribute(name, String(value)));
    return node;
  };
  const el = (doc, tag, text, attrs) => {
    const node = doc.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    Object.entries(attrs || {}).forEach(([name, value]) => node.setAttribute(name, String(value)));
    return node;
  };

  // Integers with thousands separators; never locale-dependent.
  const count = (value) => String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  // count / total as a percentage, one decimal, rounded half to even, computed
  // in exact integer arithmetic (count * 1000 stays far below 2^53).
  const share = (numerator, denominator) => {
    if (!denominator) return "not available";
    const scaled = numerator * 1000;
    let tenths = Math.floor(scaled / denominator);
    const remainder = scaled - tenths * denominator;
    if (remainder * 2 > denominator || (remainder * 2 === denominator && tenths % 2 === 1)) tenths += 1;
    return `${Math.floor(tenths / 10)}.${tenths % 10}%`;
  };
  const binText = (row) => (row.upper === null || row.upper === undefined
    ? `${count(row.lower)} and over`
    : `${count(row.lower)} to ${count(row.upper - 1)}`);

  // The bin with the largest COUNT (not the tallest bar); first on ties.
  const mostCommon = (rows) => rows.reduce((best, row, index) => (row.count > rows[best].count ? index : best), 0);

  const niceStep = (maximum, targetTicks) => {
    const raw = maximum / targetTicks;
    const power = 10 ** Math.floor(Math.log10(raw));
    const scaled = raw / power;
    const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
    return nice * power;
  };
  const percentPerBp = (value) => {
    const percent = value * 100;
    if (percent === 0) return "0%";
    const digits = percent >= 1 ? 1 : percent >= 0.1 ? 2 : 3;
    return `${percent.toFixed(digits)}%`;
  };

  // Geometry shared by both chart forms.
  const model = (rows, eligible) => {
    const finite = rows.filter((row) => row.upper !== null && row.upper !== undefined);
    const open = rows.find((row) => row.upper === null || row.upper === undefined) || null;
    const xMax = open ? open.lower + OPEN_DRAW_BP : finite[finite.length - 1].upper;
    const bars = rows.map((row, index) => {
      const isOpen = row.upper === null || row.upper === undefined;
      const width = isOpen ? OPEN_DRAW_BP : row.upper - row.lower;
      const fraction = eligible ? row.count / eligible : 0;
      return { row, index, isOpen, x0: row.lower, x1: row.lower + width, density: fraction / width };
    });
    const peak = Math.max(...bars.map((bar) => bar.density), 0);
    return { bars, xMax, peak, open, common: mostCommon(rows) };
  };

  const MAJOR_TICKS = new Set([0, 200, 500, 1000]);

  // Label rows above the plot: each label goes in the first row where it does
  // not overlap the previous label's extent.
  const placeLabels = (bars, xScale, charWidth, common) => {
    const rowsEnd = [];
    return bars.map((bar) => {
      const shareLine = bar.shareText;
      const countLine = `n ${count(bar.row.count)}`;
      const texts = [shareLine, countLine];
      if (bar.index === common) texts.push("most common bin");
      const width = Math.max(...texts.map((text) => text.length)) * charWidth;
      const center = xScale((bar.x0 + bar.x1) / 2);
      const left = center - width / 2;
      let lane = rowsEnd.findIndex((end) => left > end + 6);
      if (lane === -1) {
        lane = rowsEnd.length;
        rowsEnd.push(-Infinity);
      }
      rowsEnd[lane] = center + width / 2;
      return { bar, texts, center, lane };
    });
  };

  const histogramSvg = (doc, data) => {
    const { rows, eligible, compact, idPrefix } = data;
    const geometry = model(rows, eligible);
    const many = rows.length > MANY_BINS;
    // A narrower drawing below 42rem keeps the text legible at phone width.
    const width = compact ? 380 : 720;
    const margin = compact ? { left: 70, right: 12, bottom: 64 } : { left: 72, right: 20, bottom: 64 };
    const plotWidth = width - margin.left - margin.right;
    const xScale = (bp) => margin.left + (bp / geometry.xMax) * plotWidth;
    geometry.bars.forEach((bar) => { bar.shareText = share(bar.row.count, eligible); });
    const showLabels = !many && !compact;
    const labels = showLabels ? placeLabels(geometry.bars, xScale, 6.6, geometry.common) : [];
    const lanes = labels.length ? Math.max(...labels.map((item) => item.lane)) + 1 : 0;
    const laneHeight = 44;
    const top = 24 + lanes * laneHeight;
    const plotHeight = compact ? 220 : 260;
    const height = top + plotHeight + margin.bottom;
    const baseline = top + plotHeight;
    const yMax = geometry.peak > 0 ? geometry.peak * 1.08 : 1;
    const yScale = (density) => baseline - (density / yMax) * plotHeight;

    const root = svg(doc, "svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      class: "chart-svg",
      "aria-labelledby": `${idPrefix}-title ${idPrefix}-desc`,
      "aria-describedby": `${idPrefix}-table`,
      "data-chart": many ? "line" : "bars",
    });
    const title = svg(doc, "title", { id: `${idPrefix}-title` });
    title.textContent = "Fragment length: share of eligible alignments per bp, by aligned reference span";
    const desc = svg(doc, "desc", { id: `${idPrefix}-desc` });
    const commonRow = rows[geometry.common];
    desc.textContent = `${rows.length} bins over aligned reference span in bp; n = ${count(eligible)} eligible alignments. `
      + `The most common bin is ${binText(commonRow)} bp with ${share(commonRow.count, eligible)} of eligible alignments. `
      + "Bar area is the share; the table after the chart has the exact counts.";
    root.append(title, desc);

    const defs = svg(doc, "defs");
    const pattern = svg(doc, "pattern", {
      id: `${idPrefix}-hatch`, patternUnits: "userSpaceOnUse", width: 8, height: 8, patternTransform: "rotate(45)",
    });
    pattern.append(svg(doc, "rect", { width: 8, height: 8, class: "hatch-ground" }));
    pattern.append(svg(doc, "line", { x1: 0, y1: 0, x2: 0, y2: 8, class: "hatch-line" }));
    defs.append(pattern);
    root.append(defs);

    // Y grid and ticks (share of eligible alignments per bp).
    const step = niceStep(yMax, 4);
    for (let value = 0; value <= yMax + step * 1e-9; value += step) {
      const y = yScale(value);
      root.append(svg(doc, "line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "grid" }));
      const label = svg(doc, "text", { x: margin.left - 8, y: y + 4, class: "tick-label", "text-anchor": "end" });
      label.textContent = percentPerBp(value);
      root.append(label);
    }

    if (many) {
      const points = geometry.bars.map((bar) => `${xScale(bar.x0).toFixed(2)},${yScale(bar.density).toFixed(2)} ${xScale(bar.x1).toFixed(2)},${yScale(bar.density).toFixed(2)}`);
      root.append(svg(doc, "path", { d: `M ${points.join(" L ")}`, class: "density-line", fill: "none" }));
    } else {
      geometry.bars.forEach((bar) => {
        const x = xScale(bar.x0);
        const w = Math.max(xScale(bar.x1) - x, 1);
        const y = yScale(bar.density);
        root.append(svg(doc, "rect", {
          x: x.toFixed(2),
          y: y.toFixed(2),
          width: w.toFixed(2),
          height: Math.max(baseline - y, 0).toFixed(2),
          class: bar.isOpen ? "bar bar-open" : "bar",
          fill: bar.isOpen ? `url(#${idPrefix}-hatch)` : "currentColor",
          "data-bin": bar.index,
          "data-most-common": bar.index === geometry.common ? "true" : "false",
        }));
      });
    }

    // Per-bar labels in lanes above the plot, with a leader line to the bar.
    labels.forEach((item) => {
      const group = svg(doc, "g", { class: "bar-label", "data-bin": item.bar.index });
      const yText = 20 + item.lane * laneHeight;
      item.texts.forEach((text, line) => {
        const node = svg(doc, "text", {
          x: item.center.toFixed(2),
          y: yText + line * 13,
          "text-anchor": "middle",
          class: line === 2 ? "label-common" : "label-text",
        });
        node.textContent = text;
        group.append(node);
      });
      const barTop = yScale(item.bar.density);
      const leaderStart = yText + (item.texts.length - 1) * 13 + 4;
      if (barTop - leaderStart > 4) {
        group.append(svg(doc, "line", {
          x1: item.center.toFixed(2), x2: item.center.toFixed(2), y1: leaderStart, y2: (barTop - 2).toFixed(2), class: "leader",
        }));
      }
      root.append(group);
    });

    // X axis: ticks at bin edges (thinned to 0, 200, 500, 1000 when compact or
    // for many bins), plus the open bin's label under its hatched bar.
    root.append(svg(doc, "line", { x1: margin.left, x2: width - margin.right, y1: baseline, y2: baseline, class: "axis" }));
    const edges = [...new Set(rows.flatMap((row) => (row.upper === null || row.upper === undefined ? [row.lower] : [row.lower, row.upper])))];
    const thin = compact || many;
    edges.forEach((edge) => {
      const major = MAJOR_TICKS.has(edge);
      if (thin && !major) return;
      const x = xScale(edge);
      root.append(svg(doc, "line", { x1: x, x2: x, y1: baseline, y2: baseline + 5, class: "axis" }));
      const label = svg(doc, "text", { x, y: baseline + 18, "text-anchor": "middle", class: major ? "tick-label" : "tick-label tick-minor" });
      label.textContent = count(edge);
      root.append(label);
    });
    if (geometry.open) {
      const x = xScale(geometry.open.lower + OPEN_DRAW_BP / 2);
      const label = svg(doc, "text", { x, y: baseline + 32, "text-anchor": "middle", class: "tick-label" });
      label.textContent = `${count(geometry.open.lower)}+ (open)`;
      root.append(label);
    }
    const xTitle = svg(doc, "text", { x: margin.left + plotWidth / 2, y: height - 8, "text-anchor": "middle", class: "axis-title" });
    xTitle.textContent = "Aligned reference span (bp)";
    const yTitle = svg(doc, "text", {
      x: -(top + plotHeight / 2), y: 13, transform: "rotate(-90)", "text-anchor": "middle", class: "axis-title",
    });
    yTitle.textContent = "Share of eligible alignments per bp";
    root.append(xTitle, yTitle);
    return { root, geometry, labelled: labels.length };
  };

  // The exact table: one row per bin, or 10-bp groups for many bins.
  const groupRows = (rows) => {
    const grouped = [];
    rows.forEach((row) => {
      const open = row.upper === null || row.upper === undefined;
      const start = open ? row.lower : Math.floor(row.lower / GROUP_BP) * GROUP_BP;
      const last = grouped[grouped.length - 1];
      if (!open && last && !last.open && last.lower === start) {
        last.count += row.count;
        last.upper = Math.max(last.upper, row.upper);
      } else {
        grouped.push({ lower: start, upper: open ? null : row.upper, count: row.count, open });
      }
    });
    return grouped;
  };

  const histogramTable = (doc, data, showEvery) => {
    const { rows, eligible, idPrefix } = data;
    const many = rows.length > MANY_BINS;
    const shown = many && !showEvery ? groupRows(rows) : rows;
    const table = el(doc, "table", null, { id: `${idPrefix}-table`, class: "data-table" });
    const caption = el(doc, "caption", many && !showEvery
      ? "Counts per bin, grouped into 10 bp rows (exact)"
      : "Counts per bin (exact)");
    const head = el(doc, "thead");
    const headRow = el(doc, "tr");
    ["Aligned reference span (bp)", "Count", "Share of eligible", "Note"].forEach((text) => headRow.append(el(doc, "th", text, { scope: "col" })));
    head.append(headRow);
    const body = el(doc, "tbody");
    const common = mostCommon(rows);
    shown.forEach((row) => {
      const open = row.upper === null || row.upper === undefined;
      const tr = el(doc, "tr", null, { "data-bin-row": "true" });
      tr.append(el(doc, "th", binText(row), { scope: "row" }));
      tr.append(el(doc, "td", count(row.count), { class: "num" }));
      tr.append(el(doc, "td", share(row.count, eligible), { class: "num" }));
      const notes = [];
      if (open) notes.push("open bin, width not defined; share is exact");
      if (!many && rows[common] === row) notes.push("most common bin");
      tr.append(el(doc, "td", notes.join("; ")));
      body.append(tr);
    });
    const foot = el(doc, "tfoot");
    const total = el(doc, "tr");
    total.append(el(doc, "th", "All eligible alignments", { scope: "row" }));
    total.append(el(doc, "td", count(eligible), { class: "num" }));
    total.append(el(doc, "td", eligible ? "100.0%" : "not available", { class: "num" }));
    total.append(el(doc, "td", ""));
    foot.append(total);
    table.append(caption, head, body, foot);
    return table;
  };

  window.TracebackChart = Object.freeze({
    count,
    share,
    binText,
    mostCommon,
    histogramSvg,
    histogramTable,
    MANY_BINS,
  });
})();
