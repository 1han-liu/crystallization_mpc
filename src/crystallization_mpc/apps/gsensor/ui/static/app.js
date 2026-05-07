const statusText = document.querySelector("#status-text");
const initializedText = document.querySelector("#initialized-text");
const paramsBlock = document.querySelector("#params-block");
const messageBlock = document.querySelector("#message-block");
const refreshButton = document.querySelector("#refresh-button");
const shell = document.querySelector(".shell");
const toggleParametersButton = document.querySelector("#toggle-parameters");
const paramsForm = document.querySelector("#params-form");
const fieldTemplate = document.querySelector("#param-field-template");
const applyParamsButton = document.querySelector("#apply-params");
const resetParamsButton = document.querySelector("#reset-params");
const paramSaveStatus = document.querySelector("#param-save-status");

const state = {
  paramsVersion: 1,
  paramMeta: {},
  drawerOpen: false,
  actionInFlight: false,
};

function setDrawerOpen(open) {
  state.drawerOpen = open;
  shell.classList.toggle("drawer-open", open);
}

function parseFieldValue(rawValue) {
  const trimmed = rawValue.trim();
  if (trimmed === "") {
    return null;
  }
  try {
    return JSON.parse(trimmed);
  } catch (error) {
    return trimmed;
  }
}

function formatFieldValue(value) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

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

function renderForm(params) {
  paramsForm.innerHTML = "";
  const entries = Object.entries(params || {})
    .map(([key, value], index) => ({ key, value, index, meta: state.paramMeta[key] || {} }))
    .filter(({ meta }) => {
      const publishTo = Array.isArray(meta.publish_to) ? meta.publish_to : [];
      const uiMeta = meta.ui || {};
      return publishTo.includes("gsensor") && uiMeta.visible !== false;
    })
    .sort((left, right) => {
      const leftOrder = Number.isFinite(left.meta?.ui?.order) ? left.meta.ui.order : Number.MAX_SAFE_INTEGER;
      const rightOrder = Number.isFinite(right.meta?.ui?.order) ? right.meta.ui.order : Number.MAX_SAFE_INTEGER;
      if (leftOrder !== rightOrder) {
        return leftOrder - rightOrder;
      }
      return left.index - right.index;
    });

  const groups = [];
  const bySection = new Map();
  entries.forEach((entry) => {
    const section = entry.meta.section || "General";
    if (!bySection.has(section)) {
      const group = { section, items: [] };
      bySection.set(section, group);
      groups.push(group);
    }
    bySection.get(section).items.push(entry);
  });

  groups.forEach((group) => {
    const wrapper = document.createElement("section");
    wrapper.className = "param-group";

    const title = document.createElement("h3");
    title.className = "param-group-title";
    title.textContent = group.section;
    wrapper.appendChild(title);

    group.items.forEach(({ key, value, meta }) => {
      const field = fieldTemplate.content.firstElementChild.cloneNode(true);
      const label = field.querySelector(".field-key");
      const badges = field.querySelector(".field-badges");
      const description = field.querySelector(".field-description");
      const input = field.querySelector(".field-input");

      label.textContent = meta.label || key;
      description.textContent = meta.description || "";
      description.classList.toggle("is-empty", !meta.description);

      const badgeItems = [];
      if (meta.unit) {
        badgeItems.push(`unit: ${meta.unit}`);
      }
      if (meta.kind) {
        badgeItems.push(meta.kind);
      }
      badges.innerHTML = badgeItems.map((item) => `<span class="field-badge">${item}</span>`).join("");
      badges.classList.toggle("is-empty", badgeItems.length === 0);

      input.dataset.key = key;
      input.value = formatFieldValue(value);
      wrapper.appendChild(field);
    });

    paramsForm.appendChild(wrapper);
  });
}

function collectForm() {
  const data = {};
  paramsForm.querySelectorAll(".field-input").forEach((input) => {
    data[input.dataset.key] = parseFieldValue(input.value);
  });
  return data;
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

async function loadParams() {
  const payload = await fetchJson("/api/params");
  state.paramsVersion = payload.version || 1;
  state.paramMeta = payload.meta || {};
  renderForm(payload.params || {});
  paramSaveStatus.textContent = `loaded from ${payload.source_file || "server"}`;
  return payload;
}

async function applyParams() {
  state.actionInFlight = true;
  applyParamsButton.disabled = true;
  resetParamsButton.disabled = true;
  paramSaveStatus.textContent = "applying";
  try {
    const payload = await fetchJson("/api/params", {
      method: "POST",
      body: JSON.stringify({
        version: state.paramsVersion,
        params: collectForm(),
      }),
    });
    state.paramsVersion = payload.version || state.paramsVersion;
    renderForm(payload.params || {});
    paramSaveStatus.textContent = `saved to ${payload.runtime_file || "runtime"}`;
    await loadStatus();
  } catch (error) {
    paramSaveStatus.textContent = error.message;
    throw error;
  } finally {
    state.actionInFlight = false;
    applyParamsButton.disabled = false;
    resetParamsButton.disabled = false;
  }
}

refreshButton.addEventListener("click", async () => {
  await loadStatus();
});

toggleParametersButton.addEventListener("click", () => {
  setDrawerOpen(!state.drawerOpen);
});

applyParamsButton.addEventListener("click", async () => {
  await applyParams().catch((error) => {
    statusText.textContent = error.message;
    statusText.className = "status error";
  });
});

resetParamsButton.addEventListener("click", async () => {
  state.actionInFlight = true;
  applyParamsButton.disabled = true;
  resetParamsButton.disabled = true;
  paramSaveStatus.textContent = "resetting";
  try {
    const payload = await fetchJson("/api/params/reset", {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.paramsVersion = payload.version || 1;
    state.paramMeta = payload.meta || {};
    renderForm(payload.params || {});
    paramSaveStatus.textContent = `reset from ${payload.source_file || "default"}`;
    await loadStatus();
  } catch (error) {
    paramSaveStatus.textContent = error.message;
    statusText.textContent = error.message;
    statusText.className = "status error";
  } finally {
    state.actionInFlight = false;
    applyParamsButton.disabled = false;
    resetParamsButton.disabled = false;
  }
});

Promise.all([loadParams(), loadStatus()]).catch((error) => {
  statusText.textContent = error.message;
  statusText.className = "status error";
  paramSaveStatus.textContent = error.message;
});

setInterval(() => {
  if (!state.actionInFlight && !document.hidden) {
    loadStatus().catch((error) => {
      statusText.textContent = error.message;
      statusText.className = "status error";
    });
  }
}, 2000);

window.addEventListener("focus", () => {
  if (state.actionInFlight) {
    return;
  }
  loadStatus().catch((error) => {
    statusText.textContent = error.message;
    statusText.className = "status error";
  });
});

document.addEventListener("visibilitychange", () => {
  if (!state.actionInFlight && !document.hidden) {
    loadStatus().catch((error) => {
      statusText.textContent = error.message;
      statusText.className = "status error";
    });
  }
});
