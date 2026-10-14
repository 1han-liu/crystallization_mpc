/* History browsing is deliberately independent of active experiment selection. */
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {
    not_exported: "Not exported", pending: "Waiting for data", exporting: "Exporting",
    completed: "Completed", partial: "Incomplete", failed: "Failed",
  };
  let selected = null;
  let requestNumber = 0;
  let loading = false;
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Request failed (${response.status})`);
    }
    return response.json();
  }
  function renderDetail(run) {
    selected = run.run_id;
    $("archive-detail").hidden = false;
    $("archive-name").textContent = run.label || "Unnamed experiment";
    $("archive-run-id").textContent = run.run_id;
    $("archive-folder").textContent = run.host_directory || run.experiment_directory || run.directory_name;
    $("archive-period").textContent = `${run.started_at || "Not started"} — ${run.ended_at || "Not ended"}`;
    const archive = run.archive || {};
    $("archive-state").textContent = labels[archive.status] || "Not exported";
    $("archive-errors").textContent = [...(archive.pending_reasons || []), ...(archive.errors || [])]
      .map((item) => typeof item === "string" ? item : item.message || item.code).join(" ");
    $("archive-configuration").textContent = JSON.stringify(run.capture?.run_configuration || {}, null, 2);
    $("archive-grafana").href = run.grafana_url;
    $("archive-export").disabled = !["completed", "error"].includes(run.status) || archive.status === "exporting";
    $("archive-export").textContent = Object.keys(run.downloads || {}).length ? "Re-export CSV files" : "Export CSV files";
    const links = $("archive-downloads");
    links.replaceChildren();
    for (const [name, url] of Object.entries(run.downloads || {})) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = url;
      link.textContent = name;
      link.download = name;
      item.append(link);
      const rows = archive.rows?.[name];
      if (rows !== undefined) item.append(document.createTextNode(` — ${rows} rows`));
      links.append(item);
    }
    if (!links.childElementCount) {
      const item = document.createElement("li");
      item.textContent = "CSV files will appear here after export.";
      links.append(item);
    }
  }
  async function loadHistory() {
    const number = ++requestNumber;
    loading = true;
    const query = new URLSearchParams({ q: $("archive-query").value });
    if ($("archive-from").value) query.set("date_from", $("archive-from").value);
    if ($("archive-to").value) query.set("date_to", $("archive-to").value);
    try {
      const payload = await request(`/api/experiment-history?${query}`);
      if (number !== requestNumber) return;
      const list = $("archive-list");
      list.replaceChildren();
      const runs = payload.experiments || [];
      $("archive-message").textContent = payload.error || `${runs.length} saved experiment(s). Browsing does not change the active run.`;
      for (const run of runs) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ghost archive-run";
        button.textContent = `${run.label || "Unnamed experiment"} · ${run.created_at} · ${labels[run.archive?.status] || "Not exported"}`;
        button.addEventListener("click", () => renderDetail(run));
        list.append(button);
      }
      const chosen = runs.find((run) => run.run_id === selected);
      if (chosen) renderDetail(chosen);
      else if (runs.length) renderDetail(runs[0]);
      else { selected = null; $("archive-detail").hidden = true; }
    } catch (error) {
      $("archive-message").textContent = `History unavailable: ${error.message}`;
    } finally { if (number === requestNumber) loading = false; }
  }
  $("archive-search").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(); });
  $("experiment-history").addEventListener("toggle", () => { if ($("experiment-history").open) loadHistory(); });
  $("archive-copy-id").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(selected); $("archive-message").textContent = "Run ID copied."; }
    catch { $("archive-message").textContent = "Clipboard unavailable. Select and copy the Run ID above."; }
  });
  $("archive-export").addEventListener("click", async () => {
    if (!selected) return;
    $("archive-export").disabled = true;
    try {
      await request(`/api/experiments/${encodeURIComponent(selected)}/export`, { method: "POST" });
      $("archive-message").textContent = "Export requested. Waiting for final records to be saved.";
    } catch (error) { $("archive-message").textContent = `Export request failed: ${error.message}`; }
    finally { await loadHistory(); }
  });
  setInterval(() => { if ($("experiment-history").open && !loading) loadHistory(); }, 5000);
  window.updateExperimentArchive = (archive, error) => {
    $("current-export-status").textContent = `CSV export: ${labels[archive?.status] || "Not exported"}${error ? ` — ${error}` : ""}`;
  };
})();
