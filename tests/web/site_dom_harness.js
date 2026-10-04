// Offline DOM harness for the local records site (chart.js + site.js).
//
// It loads both scripts into one fresh context with a small fake DOM (a real
// tree: parents, ids, attributes, events), fires "traceback:operator" as
// app.js does after an operator session binds, answers fetches from a script
// (or forwards them to a real loopback server with a session cookie), runs a
// list of steps (hash changes, clicks, checkbox changes, window focus, media
// changes), and prints one JSON report per step: the #site-view tree, the
// site dataset, the focused element, the jobs summary and every request.
// No third-party package.
// Usage: node site_dom_harness.js SCENARIO.json
"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const vm = require("vm");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const errors = [];
process.on("unhandledRejection", (error) => { errors.push(String(error && error.stack ? error.stack : error)); });

const ids = new Map();
let focused = null;

class FakeNode {
  constructor(tag, ns) {
    this.tagName = String(tag).toUpperCase();
    this.ns = ns || null;
    this.attributes = {};
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this._text = "";
  }
  set textContent(value) { this._detach(); this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  _detach() { this.children.forEach((child) => { child.parent = null; }); }
  append(...nodes) {
    nodes.forEach((node) => {
      const child = typeof node === "string" ? textNode(node) : node;
      if (child.parent) child.parent.children = child.parent.children.filter((item) => item !== child);
      child.parent = this;
      this.children.push(child);
    });
  }
  replaceChildren(...nodes) { this._detach(); this.children = []; this._text = ""; this.append(...nodes); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "id") ids.set(String(value), this);
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  dispatch(type) { (this.listeners[type] || []).forEach((handler) => handler({ type, target: this, preventDefault() {} })); }
  focus() { focused = this; }
  get options() { return this.children.filter((child) => child.tagName === "OPTION"); }
}
const textNode = (value) => {
  const node = new FakeNode("#text");
  node._text = String(value);
  return node;
};

const attached = (node) => {
  let current = node;
  while (current) {
    if (current === body) return true;
    current = current.parent;
  }
  return false;
};
const body = new FakeNode("body");
const make = (id, tag) => {
  const node = new FakeNode(tag || "div");
  node.setAttribute("id", id);
  body.append(node);
  return node;
};
const site = make("site", "section");
site.hidden = true;
site.append(make("site-live", "p"));
site.append(make("site-view", "div"));
make("status", "p");
const jobsSection = make("operator-jobs", "section");
const disclosure = make("jobs-disclosure", "details");
jobsSection.append(disclosure);
disclosure.append(make("jobs-summary", "summary"), make("jobs", "ul"));

const documentElement = new FakeNode("html");
const document = {
  documentElement,
  body,
  getElementById: (id) => {
    const node = ids.get(id);
    return node && attached(node) ? node : null;
  },
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag, ns),
  addEventListener: () => {},
};

// --- fetch ---------------------------------------------------------------------
const requests = [];
const scripted = JSON.parse(JSON.stringify(scenario.responses || {}));
const base = scenario.baseUrl ? new URL(scenario.baseUrl) : null;
const realFetch = (target) => new Promise((resolve, reject) => {
  const request = http.request(
    { host: base.hostname, port: base.port, path: target, method: "GET", headers: { Host: base.host, Cookie: scenario.cookie } },
    (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve({ ok: response.statusCode < 300, status: response.statusCode, json: async () => JSON.parse(text) });
      });
    },
  );
  request.on("error", reject);
  request.end();
});
const fakeFetch = async (target) => {
  const route = target.split("?")[0];
  const queue = scripted[route] || [{ status: 404, payload: { error: { code: "TBX-WEB-404" } } }];
  const next = queue.length > 1 ? queue.shift() : queue[0];
  if (next.delayMs) await new Promise((resolve) => setTimeout(resolve, next.delayMs));
  return { ok: next.status >= 200 && next.status < 300, status: next.status, json: async () => next.payload };
};
let pending = 0;
const fetch = async (target, init = {}) => {
  pending += 1;
  try {
    const response = await (base ? realFetch(target, init) : fakeFetch(target, init));
    requests.push({ path: target, status: response.status });
    return response;
  } finally {
    pending -= 1;
  }
};

// --- window ---------------------------------------------------------------------
const listeners = {};
const storage = {};
const media = { matches: Boolean(scenario.compact), listeners: [] };
media.addEventListener = (type, handler) => media.listeners.push(handler);
const window = {
  document,
  fetch,
  innerWidth: scenario.compact ? 390 : 1280,
  location: null,
  addEventListener: (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); },
  dispatchEvent: (event) => { (listeners[event.type] || []).forEach((handler) => handler(event)); return true; },
  matchMedia: () => media,
  sessionStorage: scenario.noStorage ? undefined : {
    getItem: (key) => (Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null),
    setItem: (key, value) => { storage[key] = String(value); },
    removeItem: (key) => { delete storage[key]; },
  },
  URLSearchParams,
};
const fire = (type, detail) => window.dispatchEvent({ type, detail });
// Assigning location.hash fires hashchange (asynchronously, as browsers do).
let currentHash = scenario.hash || "";
window.location = {
  pathname: "/",
  search: "",
  get hash() { return currentHash; },
  set hash(value) {
    const next = value && !String(value).startsWith("#") ? `#${value}` : String(value);
    if (next === currentHash) return;
    currentHash = next;
    setTimeout(() => fire("hashchange"), 0);
  },
};
const context = vm.createContext({ window, document, URLSearchParams, Date, JSON, Math, Number, Set, Map, Array, Object, String });

const flush = async () => {
  const deadline = Date.now() + 30_000;
  let idle = 0;
  while (idle < 30 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, pending ? 5 : 0));
    idle = pending ? 0 : idle + 1;
  }
};

// --- serialization and lookup ---------------------------------------------------
const walk = (node, visit) => { visit(node); node.children.forEach((child) => walk(child, visit)); };
const tree = (node) => {
  if (node.tagName === "#TEXT") return node._text;
  const out = { tag: node.tagName.toLowerCase(), attrs: node.attributes };
  if (node.hidden) out.hidden = true;
  if (node.disabled) out.disabled = true;
  if (node.checked) out.checked = true;
  const text = node._text;
  const kids = node.children.map(tree);
  out.children = text ? [text, ...kids] : kids;
  return out;
};
const find = (predicate) => {
  let found = null;
  walk(body, (node) => { if (!found && node.tagName !== "#TEXT" && predicate(node)) found = node; });
  return found;
};
const bySelector = (step) => find((node) => (step.id ? node.attributes.id === step.id : true)
  && (step.tag ? node.tagName === step.tag.toUpperCase() : true)
  && (step.attr ? node.attributes[step.attr[0]] === step.attr[1] : true));

const snapshot = (label) => ({
  label,
  view: tree(ids.get("site-view")),
  dataset: Object.assign({}, site.dataset),
  siteHidden: site.hidden,
  live: ids.get("site-live").textContent,
  focused: focused ? focused.attributes.id || focused.tagName.toLowerCase() : null,
  jobsSummary: ids.get("jobs-summary").textContent,
  jobs: ids.get("jobs").children.map((child) => child.textContent),
  storage: Object.assign({}, storage),
  requests: requests.splice(0),
});

(async () => {
  const staticDir = scenario.static;
  for (const script of ["chart.js", "site.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, script), "utf8"), context, { filename: script });
  }
  if (scenario.storage) Object.assign(storage, scenario.storage);
  if (scenario.jobs !== undefined) fire("traceback:jobs", scenario.jobs);
  fire("traceback:operator");
  await flush();
  const reports = [snapshot("start")];
  for (const step of scenario.steps || []) {
    if (step.hashes) {
      // Several route changes without waiting for responses in between.
      for (const hash of step.hashes) {
        window.location.hash = hash;
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    } else if (step.hash !== undefined) {
      window.location.hash = step.hash;
    } else if (step.click) {
      const node = bySelector(step.click);
      if (!node) throw new Error(`no node for ${JSON.stringify(step.click)}`);
      if (!node.disabled) node.dispatch("click");
    } else if (step.check) {
      const node = find((item) => item.tagName === "INPUT" && item.attributes["data-record"] === step.check);
      if (!node) throw new Error(`no checkbox for ${step.check}`);
      if (!node.disabled) {
        node.checked = step.value !== false;
        node.dispatch("change");
      }
    } else if (step.select) {
      const node = ids.get(step.select);
      node.value = step.value;
      node.dispatch("change");
    } else if (step.windowFocus) {
      fire("focus");
    } else if (step.compact !== undefined) {
      media.matches = step.compact;
      media.listeners.forEach((handler) => handler({ matches: step.compact }));
    }
    if (step.noWait) await new Promise((resolve) => setTimeout(resolve, 30));
    else await flush();
    reports.push(snapshot(step.label || JSON.stringify(step)));
  }
  await flush();
  process.stdout.write(JSON.stringify({ errors, reports }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
