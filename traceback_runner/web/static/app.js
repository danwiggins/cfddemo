(() => {
  "use strict";
  const status = document.getElementById("status");
  const jobs = document.getElementById("jobs");
  let csrfToken = null;

  const renderJobs = async () => {
    const response = await fetch("/api/v1/jobs", { credentials: "same-origin" });
    if (!response.ok) throw new Error("Local queue unavailable");
    const payload = await response.json();
    jobs.replaceChildren(...payload.jobs.map((job) => {
      const item = document.createElement("li");
      item.textContent = `${job.headline}: ${job.stage_label}`;
      return item;
    }));
    status.textContent = "Local session ready";
  };

  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrap = fragment.get("bootstrap");
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  if (!bootstrap) {
    status.textContent = "Relaunch from the local Traceback command";
    return;
  }
  fetch("/api/v1/session/bootstrap", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bootstrap }),
  }).then(async (response) => {
    if (!response.ok) throw new Error("Local session unavailable");
    csrfToken = (await response.json()).csrf_token;
    await renderJobs();
  }).catch(() => {
    csrfToken = null;
    status.textContent = "Local session unavailable; relaunch Traceback";
  });
})();
