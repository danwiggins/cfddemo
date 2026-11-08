// Traceback Lab theme: follows the system light/dark setting until the viewer
// picks one with the header toggle (remembered in this browser only). Loaded
// synchronously in <head> so the first paint uses the resolved palette.
"use strict";

(function () {
  const KEY = "traceback-lab-theme";
  const root = document.documentElement;
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let chosen = null;
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored === "light" || stored === "dark") chosen = stored;
  } catch (_error) { /* storage blocked: follow the system setting */ }

  const resolved = () => chosen || (media && media.matches ? "dark" : "light");
  function apply() {
    root.dataset.resolvedTheme = resolved();
    // Fixed accessible name; the state is carried by aria-pressed.
    document.getElementById("theme-toggle")?.setAttribute("aria-pressed", String(resolved() === "dark"));
  }
  function redraw() {
    // Canvas charts take their palette at draw time; redraw the current view.
    if (document.body && document.body.dataset.ready === "1") window.TracebackLab?.render?.();
  }
  apply();
  if (media && media.addEventListener) {
    media.addEventListener("change", () => { if (!chosen) { apply(); redraw(); } });
  }
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      chosen = resolved() === "dark" ? "light" : "dark";
      try { window.localStorage.setItem(KEY, chosen); } catch (_error) { /* this page view only */ }
      apply();
      redraw();
    });
  });
})();
