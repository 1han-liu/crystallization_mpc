/* History browsing is deliberately independent of active experiment selection. */
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {
    not_exported: "Not exported", pending: "Waiting for data", exporting: "Exporting",
    completed: "Completed", partial: "Incomplete", failed: "Failed", database: "Query from InfluxDB by Run ID",
  };
  let selected = null;
  let requestNumber = 0;
  let detailNumber = 0;
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
    $("archive-folder").textContent = run.source === "influxdb" ? "InfluxDB" : (run.host_directory || run.experiment_directory || run.directory_name || "Unavailable");
    $("archive-period").textContent = `${run.started_at || "Not started"} — ${run.ended_at || "Not ended"}`;
    const archive = run.archive || {};
    $("archive-state").textContent = labels[archive.status] || "Not exported";
    $("archive-errors").textContent = [...(archive.pending_reasons || []), ...(archive.errors || [])]
      .map((item) => typeof item === "string" ? item : item.message || item.code).join(" ");
    $("archive-configuration").textContent = JSON.stringify(run.capture?.run_configuration || {}, null, 2);
    const grafana = new URL(run.grafana_url, window.location.href);
    const loopback = ["localhost", "127.0.0.1", "[::1]"];
    if (loopback.includes(grafana.hostname) && !loopback.includes(window.location.hostname)) {
      grafana.hostname = window.location.hostname;
    }
    $("archive-grafana").href = grafana.href;
    $("archive-export").disabled = !["completed", "error"].includes(run.status) || archive.status === "exporting";
    $("archive-export").textContent = "Check database data";
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
      item.textContent = "CSV queries become available after the experiment has started and ended.";
      links.append(item);
    }
  }
  async function loadDetail(run) {
    const number = ++detailNumber;
    renderDetail(run);
    try {
      const detail = await request(`/api/experiments/${encodeURIComponent(run.run_id)}/archive`);
      if (number === detailNumber && selected === run.run_id) renderDetail(detail);
    } catch (error) {
      if (number === detailNumber && selected === run.run_id) {
        $("archive-errors").textContent = `Database details unavailable: ${error.message}`;
        $("archive-downloads").replaceChildren();
        $("archive-export").disabled = true;
      }
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
      $("archive-message").textContent = payload.error || `${runs.length} experiment(s) in InfluxDB. Search by Run ID or name. Browsing does not change the active run.`;
      for (const run of runs) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ghost archive-run";
        button.textContent = `${run.label || "Unnamed experiment"} · ${run.created_at} · ${labels[run.archive?.status] || "Not exported"}`;
        button.addEventListener("click", () => loadDetail(run));
        list.append(button);
      }
      const chosen = runs.find((run) => run.run_id === selected);
      if (chosen) await loadDetail(chosen);
      else if (runs.length) await loadDetail(runs[0]);
      else { selected = null; $("archive-detail").hidden = true; }
    } catch (error) {
      if (number === requestNumber) {
        $("archive-message").textContent = `History unavailable: ${error.message}`;
        $("archive-list").replaceChildren();
        $("archive-detail").hidden = true;
        selected = null;
        detailNumber++;
      }
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
    const runId = selected;
    $("archive-export").disabled = true;
    try {
      const result = await request(`/api/experiments/${encodeURIComponent(runId)}/export`, { method: "POST" });
      const detail = await request(`/api/experiments/${encodeURIComponent(runId)}/archive`);
      if (selected !== runId) return;
      renderDetail({ ...detail, archive: result.archive });
      $("archive-message").textContent = result.archive?.status === "partial" ? "Database query completed with missing or unconfirmed data; review the details." : "Database data checked. CSV links query InfluxDB directly.";
    } catch (error) { if (selected === runId) $("archive-message").textContent = `Database query failed: ${error.message}`; }
    finally { if (selected === runId) $("archive-export").disabled = false; }
  });
  setInterval(() => { if ($("experiment-history").open && !loading) loadHistory(); }, 5000);
  window.updateExperimentArchive = (archive, error) => {
    $("current-export-status").textContent = `CSV export: ${labels[archive?.status] || "Not exported"}${error ? ` — ${error}` : ""}`;
  };
})();
