// Live progress for a running job: poll the status endpoint and reload when it finishes.
(function () {
  const panel = document.getElementById("progress");
  if (!panel) return;
  const run = panel.dataset.run;
  const step = document.getElementById("progress-step");
  const elapsed = document.getElementById("progress-elapsed");
  let seconds = parseInt(elapsed.textContent, 10) || 0;

  setInterval(function () { seconds += 1; elapsed.textContent = String(seconds); }, 1000);

  async function poll() {
    try {
      const response = await fetch("/runs/" + encodeURIComponent(run) + "/status",
                                   { headers: { "Accept": "application/json" } });
      if (response.ok) {
        const data = await response.json();
        if (data.step) step.textContent = data.step;
        if (typeof data.elapsed === "number") seconds = data.elapsed;
        if (!data.working) { window.location.reload(); return; }
      }
    } catch (err) { /* the server may be restarting; keep polling */ }
    setTimeout(poll, 2500);
  }
  setTimeout(poll, 2500);
})();
