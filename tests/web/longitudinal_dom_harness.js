// Minimal offline DOM harness for the packaged E12 view (longitudinal.js).
// It implements only the DOM surface the view uses, scripts fetch responses,
// fakes timers and the clock, and prints one JSON report.  No network, no
// third-party package.  Usage: node longitudinal_dom_harness.js SCENARIO.json
"use strict";

const fs = require("fs");
const path = require("path");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let fakeNow = 1_000_000;
Date.now = () => fakeNow;

class FakeNode {
  constructor(doc, tag, ns) {
    this.ownerDocument = doc;
    this.tagName = String(tag).toUpperCase();
    this.namespaceURI = ns || "xhtml";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.name = "";
    this.value = "";
    this.type = "";
    this.id = "";
    this.className = "";
    this.parent = null;
    this._text = "";
  }
  append(...nodes) {
    nodes.forEach((node) => {
      const child = typeof node === "string" ? textNode(this.ownerDocument, node) : node;
      child.parent = this;
      this.children.push(child);
    });
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join(" ");
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "id") this.id = String(value);
    if (name === "class") this.className = String(value);
    if (name === "type") this.type = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    event.preventDefault = event.preventDefault || (() => { event.defaultPrevented = true; });
    (this.listeners[event.type] || []).forEach((handler) => handler(event));
    return true;
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  scrollIntoView() {
    this.scrolled = true;
  }
  descendants() {
    const found = [];
    const walk = (node) => node.children.forEach((child) => { found.push(child); walk(child); });
    walk(this);
    return found;
  }
  querySelectorAll(selector) {
    const match = /^input\[name="([a-z_]+)"\]:checked$/.exec(selector);
    if (!match) throw new Error(`unsupported selector ${selector}`);
    return this.descendants().filter((node) => node.tagName === "INPUT" && node.name === match[1] && node.checked);
  }
}

const textNode = (doc, value) => {
  const node = new FakeNode(doc, "#text");
  node._text = value;
  return node;
};

class FakeDocument {
  constructor() {
    this.body = new FakeNode(this, "body");
    this.listeners = {};
    this.activeElement = this.body;
    this.visibilityState = "visible";
  }
  createElement(tag) { return new FakeNode(this, tag); }
  createElementNS(ns, tag) { return new FakeNode(this, tag, ns); }
  getElementById(id) {
    return [this.body, ...this.body.descendants()].find((node) => node.id === id) || null;
  }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  dispatch(type) { (this.listeners[type] || []).forEach((handler) => handler({ type })); }
}

const serialize = (node) => ({
  tag: node.tagName,
  ns: node.namespaceURI,
  id: node.id || undefined,
  attrs: node.attributes,
  text: node._text || undefined,
  hidden: node.hidden || undefined,
  children: node.children.map(serialize),
});

const buildDocument = (layout) => {
  const doc = new FakeDocument();
  const nodes = {};
  layout.elements.forEach((item) => {
    const node = doc.createElement(item.tag);
    node.setAttribute("id", item.id);
    node.hidden = Boolean(item.hidden);
    node.disabled = Boolean(item.disabled);
    nodes[item.id] = node;
  });
  layout.elements.forEach((item) => {
    const parent = item.parent ? nodes[item.parent] : doc.body;
    parent.append(nodes[item.id]);
  });
  layout.checkboxes.forEach((item) => {
    const input = doc.createElement("input");
    input.type = "checkbox";
    input.name = item.name;
    input.value = item.value;
    nodes["lg-filters"].append(input);
  });
  return doc;
};

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

const run = async () => {
  const api = require(path.resolve(scenario.script));
  const report = { pure: {}, snapshots: [], fetches: [] };
  if (scenario.pure) {
    const p = scenario.pure;
    report.pure.states = api.STATES;
    report.pure.actions = api.ACTIONS;
    report.pure.classify = p.workspaces.map((item) => api.classifyWorkspace(item));
    report.pure.charts = p.workspaces.map((item) => api.chartModel(item));
    report.pure.drawer = [1440, 1024, 1023, 800, 672, 671, 375].map((width) => api.drawerMode(width));
    report.pure.problems = ["permission_denied", "authority_stale", "read_conflict", "integrity_failure", "save_unavailable", "unknown"].map(
      (code) => api.problemFor(code, code === "save_unavailable" ? "registry_full" : undefined));
    // Focus containment over a fake sheet with three controls.
    const doc = new FakeDocument();
    const sheet = doc.createElement("aside");
    const first = doc.createElement("button");
    const middle = doc.createElement("a");
    middle.setAttribute("href", "#x");
    const disabled = doc.createElement("button");
    disabled.disabled = true;
    const last = doc.createElement("select");
    sheet.append(first, middle, disabled, last);
    const name = (node) => (node === first ? "first" : node === last ? "last" : node === middle ? "middle" : node ? "other" : null);
    report.pure.focus = {
      count: api.focusables(sheet).length,
      tabFromLast: name(api.containFocus(sheet, last, false)),
      shiftTabFromFirst: name(api.containFocus(sheet, first, true)),
      tabFromMiddle: name(api.containFocus(sheet, middle, false)),
      fromOutside: name(api.containFocus(sheet, doc.body, false)),
    };
    // Render every workspace into fresh containers.
    report.pure.renders = p.workspaces.map((workspace) => {
      const rdoc = new FakeDocument();
      const ui = {};
      ["identity", "outcome", "strip", "rows", "chart", "covariates", "drawerBody"].forEach((key) => {
        ui[key] = rdoc.createElement("div");
      });
      api.renderWorkspace(rdoc, ui, workspace, { authorityLabel: "current" }, () => {});
      const out = {};
      Object.entries(ui).forEach(([key, node]) => { out[key] = serialize(node); out[`${key}Text`] = node.textContent; });
      return out;
    });
  }
  if (scenario.controller) {
    const c = scenario.controller;
    const doc = buildDocument(c.layout);
    const responses = JSON.parse(JSON.stringify(c.responses));
    const intervals = [];
    const listeners = {};
    const win = {
      document: doc,
      innerWidth: c.innerWidth || 1280,
      AbortController,
      URLSearchParams,
      setInterval: (fn) => { intervals.push(fn); return intervals.length; },
      clearInterval: (id) => { intervals[id - 1] = null; },
      addEventListener: (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); },
      fetch: (url, init) => {
        const route = url.split("?")[0];
        report.fetches.push({ method: init.method, url, body: init.body ? JSON.parse(init.body) : null, csrf: init.headers["X-Traceback-CSRF"] || null });
        const queue = responses[route] || [];
        const next = queue.length > 1 ? queue.shift() : queue[0];
        if (!next || next.pending) {
          return new Promise((_, reject) => {
            init.signal.addEventListener("abort", () => {
              const error = new Error("aborted");
              error.name = "AbortError";
              reject(error);
            });
          });
        }
        return Promise.resolve({ status: next.status, json: async () => next.payload });
      },
    };
    api.install(win);
    const byId = (id) => doc.getElementById(id);
    const snapshot = (label) => {
      const section = byId("longitudinal");
      report.snapshots.push({
        label,
        state: section.dataset.state,
        busy: section.getAttribute("aria-busy"),
        sectionHidden: section.hidden,
        status: byId("lg-status").textContent,
        problemHidden: byId("lg-problem").hidden,
        problem: byId("lg-problem-text").textContent,
        fix: byId("lg-problem-fix").textContent,
        retryHidden: byId("lg-retry").hidden,
        resultsHidden: byId("lg-results").hidden,
        diffHidden: byId("lg-diff").hidden,
        showResultsDisabled: byId("lg-show-results").disabled,
        saveDisabled: byId("lg-save").disabled,
        releaseDisabled: byId("lg-release").disabled,
        exportDisabled: byId("lg-export").disabled,
        drawerHidden: byId("lg-drawer").hidden,
        drawerModal: byId("lg-drawer").getAttribute("aria-modal"),
        drawerMode: byId("lg-results").dataset.drawer || null,
        active: doc.activeElement ? (doc.activeElement.id || doc.activeElement.textContent) : null,
        rows: byId("lg-rows").children.length,
        resultsText: byId("lg-results").textContent,
        diffText: byId("lg-diff-body").textContent,
        receipt: byId("lg-receipt").textContent,
        saved: byId("lg-saved-list").textContent,
        paths: byId("lg-chart-body").descendants().filter((node) => node.tagName === "PATH").length,
        options: Object.fromEntries(["lg-cohort", "lg-measurement", "lg-anchor-policy", "lg-anchor", "lg-d09"].map(
          (id) => [id, byId(id).children.map((node) => node.value)])),
      });
    };
    for (const step of c.steps) {
      if (step.do === "bind") {
        (listeners["traceback:longitudinal"] || []).forEach((handler) => handler({ detail: { csrfToken: "csrf-token" } }));
      } else if (step.do === "select") {
        const node = byId(step.id);
        node.value = step.value;
        node.dispatchEvent({ type: "change" });
      } else if (step.do === "check") {
        const box = byId("lg-filters").children.find((node) => node.name === step.name && node.value === step.value);
        box.checked = true;
        byId("lg-journey").dispatchEvent({ type: "change" });
      } else if (step.do === "click") {
        byId(step.id).dispatchEvent({ type: "click" });
      } else if (step.do === "submit") {
        byId("lg-journey").dispatchEvent({ type: "submit" });
      } else if (step.do === "details") {
        const buttons = byId("lg-rows").descendants().filter((node) => node.className === "lg-details");
        buttons[step.index].dispatchEvent({ type: "click" });
      } else if (step.do === "clickText") {
        const target = [doc.body, ...doc.body.descendants()].find((node) => node.tagName === "BUTTON" && node._text.startsWith(step.text));
        target.dispatchEvent({ type: "click" });
      } else if (step.do === "key") {
        const event = { type: "keydown", key: step.key, shiftKey: Boolean(step.shift) };
        if (step.focus) byId(step.focus).focus();
        byId("lg-drawer").dispatchEvent(event);
      } else if (step.do === "tick") {
        fakeNow += step.ms;
        intervals.filter(Boolean).forEach((fn) => fn());
      } else if (step.do === "hide") {
        doc.visibilityState = "hidden";
        doc.dispatch("visibilitychange");
      } else if (step.do === "show") {
        doc.visibilityState = "visible";
        doc.dispatch("visibilitychange");
      } else if (step.do === "respond") {
        responses[step.route] = [step.response];
      } else if (step.do === "width") {
        win.innerWidth = step.value;
      }
      await flush();
      if (step.snapshot) snapshot(step.snapshot);
    }
  }
  process.stdout.write(JSON.stringify(report));
};

run().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
