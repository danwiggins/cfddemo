// Minimal offline DOM harness for the packaged page (app.js + longitudinal.js).
// It loads both scripts, in the page's order, into one fresh context with a
// fake document and window that deliver events for real.  Fetches are either
// answered from a script or forwarded to a real loopback server (with a cookie
// jar, Host and Origin, as the browser does).  It prints one JSON report: every
// request, the status lines, which page sections are hidden, the page's
// session datasets and any script error.  No third-party package.
// Usage: node app_dom_harness.js SCENARIO.json
"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const vm = require("vm");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const errors = [];
process.on("unhandledRejection", (error) => { errors.push(String(error && error.stack ? error.stack : error)); });

class FakeNode {
  constructor(id, tag) {
    this.id = id;
    this.tagName = String(tag).toUpperCase();
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.options = [];
    this.selectedIndex = 0;
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent || "").join(" "); }
  append(...nodes) { this.children.push(...nodes.map((node) => (typeof node === "string" ? textNode(node) : node))); }
  replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  querySelectorAll() { return []; }
  focus() {}
  cloneNode() { return new FakeNode(this.id, this.tagName); }
}
const textNode = (value) => {
  const node = new FakeNode("", "#text");
  node._text = String(value);
  return node;
};

// Elements are created on first lookup; the longitudinal section starts
// hidden, as in index.html.
const nodes = {};
const byId = (id) => {
  if (!nodes[id]) {
    nodes[id] = new FakeNode(id, "div");
    if (id === "longitudinal") nodes[id].hidden = true;
  }
  return nodes[id];
};
const documentElement = new FakeNode("html", "html");
const document = {
  documentElement,
  visibilityState: "visible",
  getElementById: byId,
  createElement: (tag) => new FakeNode("", tag),
  createElementNS: (ns, tag) => new FakeNode("", tag),
  addEventListener: () => {},
};

const requests = [];
const scripted = JSON.parse(JSON.stringify(scenario.responses || {}));
const jar = {};
const base = scenario.baseUrl ? new URL(scenario.baseUrl) : null;

const realFetch = (target, init) => new Promise((resolve, reject) => {
  const headers = Object.assign({}, init.headers || {});
  headers.Host = base.host;
  if ((init.method || "GET") !== "GET") headers.Origin = scenario.baseUrl;
  const cookie = Object.entries(jar).map(([name, value]) => `${name}=${value}`).join("; ");
  if (cookie) headers.Cookie = cookie;
  if (init.body) headers["Content-Length"] = Buffer.byteLength(init.body);
  const request = http.request(
    { host: base.hostname, port: base.port, path: target, method: init.method || "GET", headers },
    (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        (response.headers["set-cookie"] || []).forEach((line) => {
          const [pair] = line.split(";");
          const index = pair.indexOf("=");
          jar[pair.slice(0, index)] = pair.slice(index + 1);
        });
        const text = Buffer.concat(chunks).toString("utf8");
        resolve({
          ok: response.statusCode >= 200 && response.statusCode < 300,
          status: response.statusCode,
          json: async () => JSON.parse(text),
        });
      });
    },
  );
  request.on("error", reject);
  if (init.body) request.write(init.body);
  request.end();
});

const fakeFetch = async (target) => {
  const route = target.split("?")[0];
  const queue = scripted[route] || [{ status: 404, payload: { error: { code: "TBX-WEB-404" } } }];
  const next = queue.length > 1 ? queue.shift() : queue[0];
  return { ok: next.status >= 200 && next.status < 300, status: next.status, json: async () => next.payload };
};

let pending = 0;
const fetch = async (target, init = {}) => {
  pending += 1;
  try {
    const response = await (base ? realFetch(target, init) : fakeFetch(target, init));
    requests.push({ method: init.method || "GET", path: target, status: response.status });
    return response;
  } finally {
    pending -= 1;
  }
};

const listeners = {};
const window = {
  document,
  fetch,
  innerWidth: 1280,
  location: { hash: scenario.hash, pathname: "/", search: "" },
  history: { replaceState: () => { window.location.hash = ""; } },
  addEventListener: (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); },
  dispatchEvent: (event) => { (listeners[event.type] || []).forEach((handler) => handler(event)); return true; },
  setInterval: () => 0,
  clearInterval: () => {},
  AbortController,
  URLSearchParams,
};

const context = vm.createContext({
  window,
  document,
  fetch,
  performance: { now: () => 0 },
  URLSearchParams,
  AbortController,
  CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
});

// Settle: no request in flight for 50 consecutive turns (bounded at 30 s).
const flush = async () => {
  const deadline = Date.now() + 30_000;
  let idle = 0;
  while (idle < 50 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, pending ? 5 : 0));
    idle = pending ? 0 : idle + 1;
  }
};

(async () => {
  for (const script of ["app.js", "longitudinal.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scenario.static, script), "utf8"), context, { filename: script });
  }
  await flush();
  const sections = ["explorer-filters", "results", "explorer-provenance", "operator-jobs", "longitudinal"];
  process.stdout.write(JSON.stringify({
    requests,
    errors,
    status: byId("status").textContent,
    lgStatus: byId("lg-status").textContent,
    hidden: Object.fromEntries(sections.map((id) => [id, byId(id).hidden])),
    dataset: documentElement.dataset,
  }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
