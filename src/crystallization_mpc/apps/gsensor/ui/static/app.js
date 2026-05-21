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

const imageFolderInput = document.querySelector("#image-folder-input");
const imageChoice = document.querySelector("#image-choice");
const loadFolderButton = document.querySelector("#load-folder");
const undoInitButton = document.querySelector("#undo-init");
const resetInitButton = document.querySelector("#reset-init");
const initCanvas = document.querySelector("#init-canvas");
const canvasEmpty = document.querySelector("#canvas-empty");
const initStepTitle = document.querySelector("#init-step-title");
const initStepPrompt = document.querySelector("#init-step-prompt");
const fullModeControls = document.querySelector("#full-mode-controls");
const cornerControls = document.querySelector("#corner-controls");
const pointList = document.querySelector("#point-list");
const initSaveStatus = document.querySelector("#init-save-status");

const initContext = initCanvas.getContext("2d");
const initImage = new Image();

const state = {
  paramsVersion: 1,
  paramMeta: {},
  drawerOpen: false,
  actionInFlight: false,
  initialization: null,
  currentImageKey: null,
  imageReady: false,
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
  initializedText.textContent = payload.initialized ? "initialized" : payload.initialization_status || "not initialized";
  initializedText.className = payload.initialized ? "status running" : "status idle";
  paramsBlock.textContent = JSON.stringify(payload.params || {}, null, 2);
  messageBlock.textContent = JSON.stringify({
    last_message: payload.last_message || null,
    last_command_message: payload.last_command_message || null,
    initialization: payload.initialization || null,
    measurement_running: payload.measurement_running,
    last_measurement_step_at: payload.last_measurement_step_at || null,
    measurement_step_count: payload.measurement_step_count || 0,
  }, null, 2);
  renderInitialization(payload.initialization || null);
}

async function loadStatus() {
  renderStatus(await fetchJson("/api/status"));
}

async function loadParams() {
  const payload = await fetchJson("/api/params");
  state.paramsVersion = payload.version || 1;
  state.paramMeta = payload.meta || {};
  renderForm(payload.params || {});
  if (payload.params?.image_folder && !imageFolderInput.value) {
    imageFolderInput.value = payload.params.image_folder;
  }
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

function renderInitialization(payload) {
  state.initialization = payload;
  const hasSession = Boolean(payload?.session_id);
  const step = payload?.current_step || null;
  initStepTitle.textContent = hasSession ? payload.status : "Not started";
  initStepPrompt.textContent = step?.prompt || (hasSession ? "Waiting for the next initialization step." : "Load an image folder to begin.");
  initSaveStatus.textContent = hasSession
    ? `${payload.selected_image || ""}`
    : "idle";

  undoInitButton.disabled = !payload?.can_undo;
  resetInitButton.disabled = !hasSession;

  fullModeControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !hasSession || step?.key !== "is_full";
    const selected = String(payload?.is_full) === button.dataset.fullMode;
    button.classList.toggle("selected", selected);
  });

  cornerControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !hasSession || step?.type !== "corner";
    button.classList.toggle("selected", payload?.corner === button.dataset.corner);
  });

  renderPointList(payload);
  maybeLoadInitializationImage(payload);
  drawInitialization();
}

function renderPointList(payload) {
  pointList.innerHTML = "";
  const points = payload?.points || {};
  const derived = payload?.derived || {};
  Object.entries(points).forEach(([key, point]) => {
    appendPointListItem(key, point, false);
  });
  Object.entries(derived).forEach(([key, point]) => {
    appendPointListItem(key, point, true);
  });
}

function appendPointListItem(key, point, computed) {
  const item = document.createElement("li");
  const coords = Array.isArray(point) ? point.slice(0, 2).map((value) => Number(value).toFixed(1)).join(", ") : "";
  item.textContent = `${computed ? "*" : ""}${key}: ${coords}`;
  pointList.appendChild(item);
}

function maybeLoadInitializationImage(payload) {
  if (!payload?.session_id) {
    state.currentImageKey = null;
    state.imageReady = false;
    initCanvas.width = 0;
    initCanvas.height = 0;
    canvasEmpty.hidden = false;
    return;
  }
  const imageKey = `${payload.session_id}:${payload.selected_image}`;
  if (state.currentImageKey === imageKey) {
    return;
  }
  state.currentImageKey = imageKey;
  state.imageReady = false;
  canvasEmpty.hidden = false;
  initImage.src = `/api/initialization/image/${payload.session_id}?_=${Date.now()}`;
}

initImage.addEventListener("load", () => {
  state.imageReady = true;
  initCanvas.width = initImage.naturalWidth;
  initCanvas.height = initImage.naturalHeight;
  canvasEmpty.hidden = true;
  drawInitialization();
});

initImage.addEventListener("error", () => {
  state.imageReady = false;
  canvasEmpty.hidden = false;
  initSaveStatus.textContent = "image load failed";
});

function drawInitialization() {
  if (!state.imageReady || !initCanvas.width || !initCanvas.height) {
    return;
  }
  initContext.clearRect(0, 0, initCanvas.width, initCanvas.height);
  initContext.drawImage(initImage, 0, 0);
  (state.initialization?.overlays || []).forEach((overlay) => {
    if (overlay.type === "line") {
      drawLine(overlay);
    } else if (overlay.type === "arrow") {
      drawArrow(overlay);
    } else if (overlay.type === "point") {
      drawPoint(overlay);
    }
  });
}

function overlayColor(role) {
  if (role === "kernel" || role === "kernel_corner") {
    return "#b7791f";
  }
  if (role === "kernel_outer") {
    return "#ffffff";
  }
  if (role === "computed") {
    return "#1f7a4d";
  }
  if (role === "normal") {
    return "#805ad5";
  }
  return "#1f5fbf";
}

function drawLine(overlay) {
  initContext.save();
  initContext.strokeStyle = overlayColor(overlay.role);
  initContext.lineWidth = 2;
  initContext.beginPath();
  initContext.moveTo(overlay.x1, overlay.y1);
  initContext.lineTo(overlay.x2, overlay.y2);
  initContext.stroke();
  initContext.restore();
}

function drawArrow(overlay) {
  drawLine(overlay);
  const angle = Math.atan2(overlay.y2 - overlay.y1, overlay.x2 - overlay.x1);
  const headLength = 10;
  initContext.save();
  initContext.strokeStyle = overlayColor(overlay.role);
  initContext.lineWidth = 2;
  initContext.beginPath();
  initContext.moveTo(overlay.x2, overlay.y2);
  initContext.lineTo(
    overlay.x2 - headLength * Math.cos(angle - Math.PI / 6),
    overlay.y2 - headLength * Math.sin(angle - Math.PI / 6),
  );
  initContext.moveTo(overlay.x2, overlay.y2);
  initContext.lineTo(
    overlay.x2 - headLength * Math.cos(angle + Math.PI / 6),
    overlay.y2 - headLength * Math.sin(angle + Math.PI / 6),
  );
  initContext.stroke();
  initContext.restore();
}

function drawPoint(overlay) {
  initContext.save();
  initContext.fillStyle = overlayColor(overlay.role);
  initContext.strokeStyle = "#111111";
  initContext.lineWidth = 2;
  initContext.beginPath();
  initContext.arc(overlay.x, overlay.y, 5, 0, Math.PI * 2);
  initContext.fill();
  initContext.stroke();
  initContext.font = "14px Arial";
  initContext.fillStyle = "#111111";
  initContext.fillText(overlay.label, overlay.x + 10, overlay.y - 10);
  initContext.restore();
}

async function submitInitializationPoint(event) {
  const payload = state.initialization;
  const step = payload?.current_step;
  if (!payload?.session_id || step?.type !== "point") {
    return;
  }
  const rect = initCanvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) * (initCanvas.width / rect.width);
  const y = (event.clientY - rect.top) * (initCanvas.height / rect.height);
  initSaveStatus.textContent = `marking ${step.label || step.key}`;
  const nextPayload = await fetchJson("/api/initialization/point", {
    method: "POST",
    body: JSON.stringify({
      session_id: payload.session_id,
      x,
      y,
    }),
  });
  renderInitialization(nextPayload);
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

loadFolderButton.addEventListener("click", async () => {
  const folder = imageFolderInput.value.trim();
  if (!folder) {
    initSaveStatus.textContent = "folder is required";
    return;
  }
  initSaveStatus.textContent = "loading";
  try {
    const payload = await fetchJson("/api/initialization/folder", {
      method: "POST",
      body: JSON.stringify({
        folder,
        image_choice: imageChoice.value,
      }),
    });
    renderInitialization(payload);
    await loadStatus();
  } catch (error) {
    initSaveStatus.textContent = error.message;
  }
});

fullModeControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-full-mode]");
  if (!button || !state.initialization?.session_id) {
    return;
  }
  const payload = await fetchJson("/api/initialization/is-full", {
    method: "POST",
    body: JSON.stringify({
      session_id: state.initialization.session_id,
      is_full: button.dataset.fullMode === "true",
    }),
  });
  renderInitialization(payload);
});

cornerControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-corner]");
  if (!button || !state.initialization?.session_id) {
    return;
  }
  const payload = await fetchJson("/api/initialization/corner", {
    method: "POST",
    body: JSON.stringify({
      session_id: state.initialization.session_id,
      corner: button.dataset.corner,
    }),
  });
  renderInitialization(payload);
});

undoInitButton.addEventListener("click", async () => {
  if (!state.initialization?.session_id) {
    return;
  }
  const payload = await fetchJson("/api/initialization/undo", {
    method: "POST",
    body: JSON.stringify({ session_id: state.initialization.session_id }),
  });
  renderInitialization(payload);
});

resetInitButton.addEventListener("click", async () => {
  const payload = await fetchJson("/api/initialization/reset", {
    method: "POST",
    body: JSON.stringify({ session_id: state.initialization?.session_id || null }),
  });
  renderInitialization(payload);
  await loadStatus();
});

initCanvas.addEventListener("click", (event) => {
  submitInitializationPoint(event).catch((error) => {
    initSaveStatus.textContent = error.message;
  });
});

window.addEventListener("resize", drawInitialization);

Promise.all([loadParams(), loadStatus()]).catch((error) => {
  statusText.textContent = error.message;
  statusText.className = "status error";
  paramSaveStatus.textContent = error.message;
});

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
