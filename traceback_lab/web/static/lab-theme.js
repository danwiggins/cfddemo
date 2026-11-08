// Traceback Lab theme: light by default; the header toggle switches to dark
// and remembers the choice in this browser only. Loaded synchronously in
// <head> so the first paint uses the chosen palette.
"use strict";

(function () {
  const KEY = "traceback-lab-theme";
  const root = document.documentElement;
  let chosen = "light";
  try {
    if (window.localStorage.getItem(KEY) === "dark") chosen = "dark";
  } catch (_error) { /* storage blocked: stay light */ }

  function apply() {
    root.dataset.resolvedTheme = chosen;
    const button = document.getElementById("theme-toggle");
    if (button) {
      const next = chosen === "dark" ? "light" : "dark";
      button.setAttribute("aria-label", `Switch to ${next} theme`);
      button.setAttribute("aria-pressed", String(chosen === "dark"));
      button.title = `Switch to ${next} theme`;
    }
  }
  apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", () => {
      chosen = chosen === "dark" ? "light" : "dark";
      try { window.localStorage.setItem(KEY, chosen); } catch (_error) { /* this page view only */ }
      apply();
      // Canvas charts take their palette at draw time; redraw the current view.
      if (document.body.dataset.ready === "1") window.TracebackLab?.render?.();
    });
  });
})();
