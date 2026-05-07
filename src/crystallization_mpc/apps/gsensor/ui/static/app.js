const statusText = document.querySelector("#status-text");
const initializedText = document.querySelector("#initialized-text");
const paramsBlock = document.querySelector("#params-block");
const messageBlock = document.querySelector("#message-block");
const refreshButton = document.querySelector("#refresh-button");

function cacheBustedUrl(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET" || !url.startsWith("/api/")) {
    return url;
  }
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}_=${Date.now()}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(cacheBustedUrl(url, options), {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function renderStatus(payload) {
  statusText.textContent = payload.active ? "running" : "idle";
  statusText.className = payload.active ? "status running" : "status idle";
  initializedText.textContent = payload.initialized ? "initialized" : "not initialized";
  initializedText.className = payload.initialized ? "status running" : "status idle";
  paramsBlock.textContent = JSON.stringify(payload.params || {}, null, 2);
  messageBlock.textContent = JSON.stringify({
    last_message: payload.last_message || null,
    last_command_message: payload.last_command_message || null,
    measurement_running: payload.measurement_running,
    last_measurement_step_at: payload.last_measurement_step_at || null,
    measurement_step_count: payload.measurement_step_count || 0,
  }, null, 2);
}

async function loadStatus() {
  renderStatus(await fetchJson("/api/status"));
}

refreshButton.addEventListener("click", async () => {
  await loadStatus();
});

loadStatus().catch((error) => {
  statusText.textContent = error.message;
  statusText.className = "status error";
});

setInterval(() => {
  if (!document.hidden) {
    loadStatus().catch((error) => {
      statusText.textContent = error.message;
      statusText.className = "status error";
    });
  }
}, 2000);

window.addEventListener("focus", () => {
  loadStatus().catch((error) => {
    statusText.textContent = error.message;
    statusText.className = "status error";
  });
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadStatus().catch((error) => {
      statusText.textContent = error.message;
      statusText.className = "status error";
    });
  }
});
