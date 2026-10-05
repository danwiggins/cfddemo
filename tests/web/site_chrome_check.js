// Real-browser check of the records site: node site_chrome_check.js CHROME LAUNCH_URL WIDTH ROUTE...
// Opens the one-use launch link in chrome-headless-shell over CDP, visits each
// hash route, and prints JSON: per route the page's scrollWidth vs innerWidth,
// the site's view state, the computed banner styling (proof the packaged CSS
// applied under the server's CSP), and every console or CSP message.  With
// SITE_SCREENSHOT_DIR set, it also saves one full-page PNG per route there.
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const [chromePath, url, width, ...routes] = process.argv.slice(2);
const port = 9600 + Math.floor(Math.random() * 300);
const chrome = spawn(chromePath, [
  "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, `--window-size=${width},900`, "about:blank",
], { stdio: "ignore" });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  let targets = null;
  for (let attempt = 0; attempt < 100 && !targets; attempt += 1) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (_) { await sleep(100); }
  }
  const page = targets.find((target) => target.type === "page");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve) => { socket.onopen = resolve; });
  let id = 0;
  const pending = {};
  const messages = [];
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.id && pending[data.id]) { pending[data.id](data); delete pending[data.id]; }
    if (data.method === "Runtime.consoleAPICalled") messages.push(data.params.args.map((arg) => arg.value).join(" "));
    if (data.method === "Runtime.exceptionThrown") messages.push("exception");
    if (data.method === "Log.entryAdded") messages.push(data.params.entry.text);
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    pending[id] = resolve;
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => (await send("Runtime.evaluate", { expression, returnByValue: true })).result.result.value;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: Number(width), height: 900, deviceScaleFactor: 1, mobile: Number(width) < 500 });
  await send("Page.navigate", { url });
  const expected = (route) => (route.startsWith("#/records/") ? "record:" : route.startsWith("#/compare") ? "compare:" : "catalog:");
  const waitForView = async (route) => {
    for (let attempt = 0; attempt < 1200; attempt += 1) {
      const state = (await evaluate("document.documentElement.dataset.viewState || ''")) || "";
      if (state.startsWith(expected(route)) && !state.endsWith(":loading")) return state;
      await sleep(50);
    }
    return "timeout";
  };
  const measure = async (route) => ({
    route,
    state: await waitForView(route),
    scrollWidth: await evaluate("document.documentElement.scrollWidth"),
    innerWidth: await evaluate("window.innerWidth"),
    bannerBorder: await evaluate("getComputedStyle(document.getElementById('banner')).borderLeftStyle"),
  });
  const shots = process.env.SITE_SCREENSHOT_DIR || "";
  const capture = async (index) => {
    if (!shots) return;
    await sleep(150);
    const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(path.join(shots, `site-${width}-${index}.png`), Buffer.from(shot.result.data, "base64"));
  };
  const results = [await measure("#/")];
  await capture(0);
  for (const [index, route] of routes.entries()) {
    await evaluate(`window.location.hash = ${JSON.stringify(route)}; true`);
    await sleep(100);
    results.push(await measure(route));
    await capture(index + 1);
  }
  process.stdout.write(JSON.stringify({ results, messages }));
  socket.close();
  chrome.kill();
})().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  chrome.kill();
  process.exit(1);
});
