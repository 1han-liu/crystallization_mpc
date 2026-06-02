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

const imageFolderPicker = document.querySelector("#image-folder-picker");
const imageChoice = document.querySelector("#image-choice");
const loadFolderButton = document.querySelector("#load-folder");
const undoInitButton = document.querySelector("#undo-init");
const resetInitButton = document.querySelector("#reset-init");
const initCanvas = document.querySelector("#init-canvas");
const canvasEmpty = document.querySelector("#canvas-empty");
const threeDPreview = document.querySelector("#three-d-preview");
const threeDPreviewTitle = document.querySelector("#three-d-preview-title");
const threeDCanvas = document.querySelector("#three-d-canvas");
const reset3DViewButton = document.querySelector("#reset-3d-view");
const threeDSnapshots = document.querySelector("#three-d-snapshots");
const threeDSnapshotGrid = document.querySelector("#three-d-snapshot-grid");
const clear3DSnapshotsButton = document.querySelector("#clear-3d-snapshots");
const initStepTitle = document.querySelector("#init-step-title");
const initStepPrompt = document.querySelector("#init-step-prompt");
const fullModeControls = document.querySelector("#full-mode-controls");
const cornerReference = document.querySelector("#corner-reference");
const cornerControls = document.querySelector("#corner-controls");
const candidateControlsWrap = document.querySelector("#candidate-controls-wrap");
const candidateControls = document.querySelector("#candidate-controls");
const pointList = document.querySelector("#point-list");
const initSaveStatus = document.querySelector("#init-save-status");

const initContext = initCanvas.getContext("2d");
const threeDContext = threeDCanvas.getContext("2d");
const initImage = new Image();
const supportedImageExtensions = new Set([".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"]);
const default3DView = { yaw: -0.65, pitch: 0.55 };

const state = {
  paramsVersion: 1,
  paramMeta: {},
  drawerOpen: false,
  actionInFlight: false,
  initialization: null,
  currentImageKey: null,
  imageReady: false,
  threeDView: {
    yaw: default3DView.yaw,
    pitch: default3DView.pitch,
    dragging: false,
    lastX: 0,
    lastY: 0,
    patchKey: null,
    viewInitialized: false,
  },
  threeDSnapshots: {
    scope: null,
    order: [],
    items: new Map(),
  },
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

function imageFilesFromPicker() {
  return Array.from(imageFolderPicker.files || []).filter((file) => {
    const lowerName = file.name.toLowerCase();
    const dotIndex = lowerName.lastIndexOf(".");
    const extension = dotIndex >= 0 ? lowerName.slice(dotIndex) : "";
    return supportedImageExtensions.has(extension);
  });
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const result = String(reader.result || "");
      resolve(result.includes(",") ? result.split(",", 2)[1] : result);
    });
    reader.addEventListener("error", () => {
      reject(reader.error || new Error(`Could not read ${file.name}`));
    });
    reader.readAsDataURL(file);
  });
}

async function uploadSelectedImageFolder(files) {
  const payloadFiles = [];
  for (const file of files) {
    payloadFiles.push({
      filename: file.webkitRelativePath || file.name,
      content_base64: await readFileAsBase64(file),
    });
  }
  return fetchJson("/api/initialization/upload-folder", {
    method: "POST",
    body: JSON.stringify({
      image_choice: imageChoice.value,
      files: payloadFiles,
    }),
  });
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
  ensure3DSnapshotScope(payload);
  state.initialization = payload;
  const hasSession = Boolean(payload?.session_id);
  const step = payload?.current_step || null;
  const selected3DChoice = payload?.selected_3d_choice;
  initStepTitle.textContent = hasSession ? payload.status : "Not started";
  initStepPrompt.textContent = step?.prompt || (selected3DChoice
    ? `Selected 3D choice ${selected3DChoice}.`
    : (hasSession ? "Waiting for the next initialization step." : "Load an image folder to begin."));
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

  cornerReference.hidden = step?.type !== "corner";

  cornerControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !hasSession || step?.type !== "corner";
    button.classList.toggle("selected", payload?.corner === button.dataset.corner);
  });

  renderCandidateControls(payload);
  renderPointList(payload);
  maybeLoadInitializationImage(payload);
  drawInitialization();
  drawSelected3DPreview();
  render3DSnapshots();
}

function renderCandidateControls(payload) {
  candidateControls.innerHTML = "";
  const candidates = payload?.candidates_3d || [];
  candidateControlsWrap.hidden = candidates.length === 0;
  candidates.forEach((candidate) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.dataset.choice = String(candidate.choice);
    button.textContent = candidate.label || `Choice ${candidate.choice}`;
    button.disabled = !["ready_for_3d_choice", "ready_for_3d"].includes(payload?.status);
    button.classList.toggle("selected", payload?.selected_3d_choice === candidate.choice);
    candidateControls.appendChild(button);
  });
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

function selected3DPatch(payload) {
  if (payload?.recovered_3d?.show_3d) {
    return payload.recovered_3d.show_3d;
  }
  if (payload?.selected_3d_choice == null) {
    return null;
  }
  const candidate = (payload?.candidates_3d || []).find((item) => item.choice === payload.selected_3d_choice);
  return candidate?.show_3d || null;
}

function reference2DPayload(patch) {
  const vertices = Array.isArray(patch?.vertices) ? patch.vertices : [];
  const fallbackVertices = vertices.map((vertex) => [
    Number(vertex?.[0]) || 0,
    Number(vertex?.[1]) || 0,
    0,
  ]);
  const reference = patch?.reference_2d || {};
  const referenceVertices = Array.isArray(reference.vertices) && reference.vertices.length
    ? reference.vertices
    : fallbackVertices;
  const edges = Array.isArray(reference.edges) && reference.edges.length
    ? reference.edges
    : defaultReferenceEdges(referenceVertices.length);
  const labels = Array.isArray(reference.labels) && reference.labels.length
    ? reference.labels
    : ["M", "W", "U", "V"].slice(0, referenceVertices.length);
  return { vertices: referenceVertices, edges, labels };
}

function defaultReferenceEdges(count) {
  const edges = [];
  for (let left = 1; left <= count; left += 1) {
    for (let right = left + 1; right <= count; right += 1) {
      edges.push([left, right]);
    }
  }
  return edges;
}

function selected3DCandidate(payload) {
  if (payload?.selected_3d_choice == null) {
    return null;
  }
  return (payload?.candidates_3d || []).find((item) => item.choice === payload.selected_3d_choice) || null;
}

function threeDSnapshotScope(payload) {
  if (!payload?.session_id || !payload?.corner) {
    return null;
  }
  return `${payload.session_id}:${payload.corner}`;
}

function ensure3DSnapshotScope(payload) {
  const scope = threeDSnapshotScope(payload);
  if (scope === state.threeDSnapshots.scope) {
    return;
  }
  state.threeDSnapshots.scope = scope;
  state.threeDSnapshots.order = [];
  state.threeDSnapshots.items = new Map();
  state.threeDView.patchKey = null;
  state.threeDView.viewInitialized = false;
  state.threeDView.yaw = default3DView.yaw;
  state.threeDView.pitch = default3DView.pitch;
}

function captureCurrent3DSnapshot() {
  const payload = state.initialization;
  const patch = selected3DPatch(payload);
  const candidate = selected3DCandidate(payload);
  if (!patch || !candidate || !threeDCanvas.width || !threeDCanvas.height) {
    return;
  }
  const key = String(candidate.choice);
  const item = {
    key,
    choice: candidate.choice,
    label: candidate.label || `Choice ${candidate.choice}`,
    image: threeDCanvas.toDataURL("image/png"),
    yaw: state.threeDView.yaw,
    pitch: state.threeDView.pitch,
  };
  if (!state.threeDSnapshots.items.has(key)) {
    state.threeDSnapshots.order.push(key);
  }
  state.threeDSnapshots.items.set(key, item);
  render3DSnapshots();
}

function clear3DSnapshots() {
  state.threeDSnapshots.order = [];
  state.threeDSnapshots.items = new Map();
  render3DSnapshots();
}

function render3DSnapshots() {
  const items = state.threeDSnapshots.order
    .map((key) => state.threeDSnapshots.items.get(key))
    .filter(Boolean);
  threeDSnapshots.hidden = items.length === 0;
  clear3DSnapshotsButton.disabled = items.length === 0;
  threeDSnapshotGrid.innerHTML = "";
  items.forEach((item) => {
    const wrapper = document.createElement("figure");
    wrapper.className = "three-d-snapshot";
    const image = document.createElement("img");
    image.src = item.image;
    image.alt = item.label;
    const caption = document.createElement("span");
    caption.textContent = `${item.label} (${formatRadians(item.yaw)}, ${formatRadians(item.pitch)})`;
    wrapper.appendChild(image);
    wrapper.appendChild(caption);
    threeDSnapshotGrid.appendChild(wrapper);
  });
}

function formatRadians(value) {
  return `${Number(value || 0).toFixed(2)} rad`;
}

function saved3DViewForSelectedCandidate() {
  const choice = state.initialization?.selected_3d_choice;
  if (choice == null) {
    return null;
  }
  const item = state.threeDSnapshots.items.get(String(choice));
  const yaw = Number(item?.yaw);
  const pitch = Number(item?.pitch);
  if (!Number.isFinite(yaw) || !Number.isFinite(pitch)) {
    return null;
  }
  return { yaw, pitch };
}

function drawSelected3DPreview() {
  const patch = selected3DPatch(state.initialization);
  const choice = state.initialization?.selected_3d_choice;
  threeDPreview.hidden = !patch;
  reset3DViewButton.disabled = !patch;
  if (!patch) {
    clear3DPreview();
    return;
  }
  threeDPreviewTitle.textContent = choice ? `Choice ${choice}` : "Selected candidate";
  applyInitial3DView(patch);
  draw3DPreview(patch);
}

function clear3DPreview() {
  threeDContext.clearRect(0, 0, threeDCanvas.width || 1, threeDCanvas.height || 1);
}

function draw3DPreview(patch) {
  const vertices = Array.isArray(patch?.vertices) ? patch.vertices : [];
  const faces = Array.isArray(patch?.faces) ? patch.faces : [];
  if (!vertices.length || !faces.length) {
    clear3DPreview();
    return;
  }
  size3DCanvas();
  const transformed = vertices.map((vertex) => transform3DVertex(vertex));
  const reference2D = reference2DPayload(patch);
  const referenceTransformed = reference2D.vertices.map((vertex) => transform3DVertex(vertex));
  const projected = project3DVertices(
    [...transformed, ...referenceTransformed],
    threeDCanvas.width,
    threeDCanvas.height,
  );
  const projectedPatchPoints = projected.points.slice(0, transformed.length);
  const projectedReferencePoints = projected.points.slice(transformed.length);
  const alpha = Math.max(0, Math.min(Number(patch.face_alpha) || 0.16, 1));
  const sortedFaces = faces.slice().sort((left, right) => averageFaceZ(transformed, right) - averageFaceZ(transformed, left));

  threeDContext.clearRect(0, 0, threeDCanvas.width, threeDCanvas.height);
  draw3DAxes(projected.scale, threeDCanvas.width, threeDCanvas.height);
  draw2DReference(projectedReferencePoints, reference2D.edges, reference2D.labels);
  threeDContext.save();
  sortedFaces.forEach((face) => {
    const points = patchFaceVertices(projectedPatchPoints, face);
    if (points.length < 3) {
      return;
    }
    const shade = faceShade(transformed, face);
    threeDContext.fillStyle = `rgba(${shade}, 38, 38, ${alpha})`;
    threeDContext.strokeStyle = "rgba(120, 20, 20, 0.72)";
    threeDContext.lineWidth = 1.25;
    threeDContext.beginPath();
    threeDContext.moveTo(points[0].x, points[0].y);
    points.slice(1).forEach((point) => threeDContext.lineTo(point.x, point.y));
    threeDContext.closePath();
    threeDContext.fill();
    threeDContext.stroke();
  });
  draw3DVertices(projectedPatchPoints);
  threeDContext.restore();
}

function applyInitial3DView(patch) {
  const choice = state.initialization?.selected_3d_choice ?? "selected";
  const key = `${choice}:${JSON.stringify(patch?.vertices || [])}`;
  if (state.threeDView.patchKey === key) {
    return;
  }
  const savedView = saved3DViewForSelectedCandidate();
  if (savedView) {
    state.threeDView.yaw = savedView.yaw;
    state.threeDView.pitch = savedView.pitch;
    state.threeDView.viewInitialized = true;
  } else if (!state.threeDView.viewInitialized) {
    const view = bestInitial3DView(patch);
    state.threeDView.yaw = view.yaw;
    state.threeDView.pitch = view.pitch;
    state.threeDView.viewInitialized = true;
  }
  state.threeDView.patchKey = key;
}

function bestInitial3DView(patch) {
  const vertices = Array.isArray(patch?.vertices) ? patch.vertices : [];
  if (vertices.length < 4) {
    return { ...default3DView };
  }
  let best = { ...default3DView };
  let bestScore = -Infinity;
  const pitchValues = [-0.95, -0.7, -0.45, 0.45, 0.7, 0.95];
  for (let yawIndex = 0; yawIndex < 24; yawIndex += 1) {
    const yaw = (Math.PI * 2 * yawIndex) / 24;
    pitchValues.forEach((pitch) => {
      const points = vertices.map((vertex) => transform3DVertexWithView(vertex, { yaw, pitch }));
      const score = projectedSpreadScore(points);
      if (score > bestScore) {
        bestScore = score;
        best = { yaw, pitch };
      }
    });
  }
  return best;
}

function size3DCanvas() {
  const rect = threeDCanvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width * scale));
  const height = Math.max(240, Math.floor(rect.height * scale));
  if (threeDCanvas.width !== width || threeDCanvas.height !== height) {
    threeDCanvas.width = width;
    threeDCanvas.height = height;
  }
}

function transform3DVertex(vertex) {
  return transform3DVertexWithView(vertex, state.threeDView);
}

function transform3DVertexWithView(vertex, view) {
  const values = Array.isArray(vertex) ? vertex : [];
  const x = Number(values[0]) || 0;
  const y = -(Number(values[1]) || 0);
  const z = Number(values[2]) || 0;
  const cosYaw = Math.cos(view.yaw);
  const sinYaw = Math.sin(view.yaw);
  const cosPitch = Math.cos(view.pitch);
  const sinPitch = Math.sin(view.pitch);
  const yawX = x * cosYaw + z * sinYaw;
  const yawZ = -x * sinYaw + z * cosYaw;
  return {
    x: yawX,
    y: y * cosPitch - yawZ * sinPitch,
    z: y * sinPitch + yawZ * cosPitch,
  };
}

function projectedSpreadScore(points) {
  const bounds = points.reduce((acc, point) => ({
    minX: Math.min(acc.minX, point.x),
    maxX: Math.max(acc.maxX, point.x),
    minY: Math.min(acc.minY, point.y),
    maxY: Math.max(acc.maxY, point.y),
  }), { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
  const area = Math.max(bounds.maxX - bounds.minX, 1) * Math.max(bounds.maxY - bounds.minY, 1);
  let minDistance = Infinity;
  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      minDistance = Math.min(minDistance, Math.hypot(points[i].x - points[j].x, points[i].y - points[j].y));
    }
  }
  return area + (Number.isFinite(minDistance) ? minDistance * 20 : 0);
}

function project3DVertices(points, width, height) {
  const bounds = points.reduce((acc, point) => ({
    minX: Math.min(acc.minX, point.x),
    maxX: Math.max(acc.maxX, point.x),
    minY: Math.min(acc.minY, point.y),
    maxY: Math.max(acc.maxY, point.y),
  }), { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
  const rangeX = Math.max(bounds.maxX - bounds.minX, 1);
  const rangeY = Math.max(bounds.maxY - bounds.minY, 1);
  const scale = Math.min(width * 0.72 / rangeX, height * 0.72 / rangeY);
  const centerX = width / 2;
  const centerY = height / 2;
  const sourceCenterX = (bounds.minX + bounds.maxX) / 2;
  const sourceCenterY = (bounds.minY + bounds.maxY) / 2;
  return {
    scale,
    points: points.map((point) => ({
      x: centerX + (point.x - sourceCenterX) * scale,
      y: centerY - (point.y - sourceCenterY) * scale,
      z: point.z,
    })),
  };
}

function indexedVertices(points, indexes) {
  if (!Array.isArray(indexes)) {
    return [];
  }
  const indexOffset = indexes.some((index) => Number(index) === 0) ? 0 : 1;
  return indexes
    .map((index) => points[Number(index) - indexOffset])
    .filter((point) => point && Number.isFinite(point.x) && Number.isFinite(point.y));
}

function patchFaceVertices(points, face) {
  return indexedVertices(points, face);
}

function averageFaceZ(points, face) {
  const facePoints = patchFaceVertices(points, face);
  if (!facePoints.length) {
    return 0;
  }
  return facePoints.reduce((sum, point) => sum + point.z, 0) / facePoints.length;
}

function faceShade(points, face) {
  const facePoints = patchFaceVertices(points, face);
  if (facePoints.length < 3) {
    return 200;
  }
  const a = vectorBetween(facePoints[0], facePoints[1]);
  const b = vectorBetween(facePoints[0], facePoints[2]);
  const normal = normalizeVector(crossProduct(a, b));
  const light = normalizeVector({ x: -0.2, y: 0.4, z: 1 });
  const brightness = Math.max(0.25, Math.abs(dotProduct(normal, light)));
  return Math.round(145 + brightness * 80);
}

function convexHull2D(points) {
  const unique = [];
  points.forEach((point) => {
    if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) {
      return;
    }
    const duplicate = unique.some((existing) => (
      Math.abs(existing.x - point.x) < 0.001 && Math.abs(existing.y - point.y) < 0.001
    ));
    if (!duplicate) {
      unique.push(point);
    }
  });
  if (unique.length <= 2) {
    return unique;
  }
  const sorted = unique.slice().sort((left, right) => (
    left.x === right.x ? left.y - right.y : left.x - right.x
  ));
  const cross = (origin, left, right) => (
    (left.x - origin.x) * (right.y - origin.y)
    - (left.y - origin.y) * (right.x - origin.x)
  );
  const lower = [];
  sorted.forEach((point) => {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) {
      lower.pop();
    }
    lower.push(point);
  });
  const upper = [];
  sorted.slice().reverse().forEach((point) => {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) {
      upper.pop();
    }
    upper.push(point);
  });
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

function draw2DReference(points) {
  const polygon = convexHull2D(points);
  if (polygon.length < 3) {
    return;
  }
  const bounds = polygon.reduce((acc, point) => ({
    minX: Math.min(acc.minX, point.x),
    maxX: Math.max(acc.maxX, point.x),
    minY: Math.min(acc.minY, point.y),
    maxY: Math.max(acc.maxY, point.y),
  }), { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
  threeDContext.save();
  const gradient = threeDContext.createLinearGradient(bounds.minX, bounds.minY, bounds.maxX, bounds.maxY);
  gradient.addColorStop(0, "rgba(247, 235, 164, 0.46)");
  gradient.addColorStop(1, "rgba(179, 176, 151, 0.30)");
  threeDContext.fillStyle = gradient;
  threeDContext.strokeStyle = "rgba(126, 116, 72, 0.72)";
  threeDContext.lineWidth = 1.4;
  threeDContext.beginPath();
  threeDContext.moveTo(polygon[0].x, polygon[0].y);
  polygon.slice(1).forEach((point) => {
    threeDContext.lineTo(point.x, point.y);
  });
  threeDContext.closePath();
  threeDContext.fill();
  threeDContext.stroke();
  threeDContext.restore();
}

function draw3DVertices(points) {
  const labels = ["M", "W", "U", "V"];
  threeDContext.font = "12px Arial";
  points.forEach((point, index) => {
    threeDContext.beginPath();
    threeDContext.fillStyle = "#111111";
    threeDContext.arc(point.x, point.y, 4, 0, Math.PI * 2);
    threeDContext.fill();
    threeDContext.fillText(labels[index] || String(index + 1), point.x + 7, point.y - 7);
  });
}

function draw3DAxes(scale, width, height) {
  const origin = { x: width - 84, y: height - 54, z: 0 };
  const axisLength = Math.max(36, Math.min(70, scale * 0.08));
  const axes = [
    { label: "x", color: "#1f5fbf", point: transform3DVertex([axisLength, 0, 0]) },
    { label: "y", color: "#1f7a4d", point: transform3DVertex([0, axisLength, 0]) },
    { label: "z", color: "#805ad5", point: transform3DVertex([0, 0, axisLength]) },
  ];
  threeDContext.save();
  threeDContext.font = "12px Arial";
  axes.forEach((axis) => {
    threeDContext.strokeStyle = axis.color;
    threeDContext.fillStyle = axis.color;
    threeDContext.lineWidth = 1.5;
    threeDContext.beginPath();
    threeDContext.moveTo(origin.x, origin.y);
    threeDContext.lineTo(origin.x + axis.point.x, origin.y - axis.point.y);
    threeDContext.stroke();
    threeDContext.fillText(axis.label, origin.x + axis.point.x + 4, origin.y - axis.point.y);
  });
  threeDContext.restore();
}

function vectorBetween(left, right) {
  return {
    x: right.x - left.x,
    y: right.y - left.y,
    z: right.z - left.z,
  };
}

function crossProduct(left, right) {
  return {
    x: left.y * right.z - left.z * right.y,
    y: left.z * right.x - left.x * right.z,
    z: left.x * right.y - left.y * right.x,
  };
}

function dotProduct(left, right) {
  return left.x * right.x + left.y * right.y + left.z * right.z;
}

function normalizeVector(vector) {
  const length = Math.hypot(vector.x, vector.y, vector.z) || 1;
  return {
    x: vector.x / length,
    y: vector.y / length,
    z: vector.z / length,
  };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(value, max));
}

function update3DViewFromPointer(event) {
  if (!state.threeDView.dragging) {
    return;
  }
  const dx = event.clientX - state.threeDView.lastX;
  const dy = event.clientY - state.threeDView.lastY;
  state.threeDView.lastX = event.clientX;
  state.threeDView.lastY = event.clientY;
  state.threeDView.yaw += dx * 0.01;
  state.threeDView.pitch = clamp(state.threeDView.pitch + dy * 0.01, -1.25, 1.25);
  drawSelected3DPreview();
}

function reset3DView() {
  const patch = selected3DPatch(state.initialization);
  const view = bestInitial3DView(patch);
  state.threeDView.yaw = view.yaw;
  state.threeDView.pitch = view.pitch;
  state.threeDView.viewInitialized = true;
  drawSelected3DPreview();
  captureCurrent3DSnapshot();
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
  const selectedFiles = imageFilesFromPicker();
  if (selectedFiles.length === 0) {
    initSaveStatus.textContent = "choose a local image folder";
    return;
  }

  initSaveStatus.textContent = `uploading ${selectedFiles.length} images`;
  try {
    const payload = await uploadSelectedImageFolder(selectedFiles);
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

candidateControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-choice]");
  if (!button || !state.initialization?.session_id) {
    return;
  }
  const nextChoice = Number(button.dataset.choice);
  if (state.initialization.selected_3d_choice != null
      && state.initialization.selected_3d_choice !== nextChoice) {
    captureCurrent3DSnapshot();
  }
  const payload = await fetchJson("/api/initialization/3d-choice", {
    method: "POST",
    body: JSON.stringify({
      session_id: state.initialization.session_id,
      choice: nextChoice,
    }),
  });
  renderInitialization(payload);
});

threeDCanvas.addEventListener("pointerdown", (event) => {
  if (!selected3DPatch(state.initialization)) {
    return;
  }
  state.threeDView.dragging = true;
  state.threeDView.lastX = event.clientX;
  state.threeDView.lastY = event.clientY;
  threeDCanvas.classList.add("is-dragging");
  threeDCanvas.setPointerCapture(event.pointerId);
});

threeDCanvas.addEventListener("pointermove", update3DViewFromPointer);

threeDCanvas.addEventListener("pointerup", (event) => {
  const wasDragging = state.threeDView.dragging;
  state.threeDView.dragging = false;
  threeDCanvas.classList.remove("is-dragging");
  if (threeDCanvas.hasPointerCapture(event.pointerId)) {
    threeDCanvas.releasePointerCapture(event.pointerId);
  }
  if (wasDragging) {
    captureCurrent3DSnapshot();
  }
});

threeDCanvas.addEventListener("pointercancel", () => {
  state.threeDView.dragging = false;
  threeDCanvas.classList.remove("is-dragging");
});

reset3DViewButton.addEventListener("click", reset3DView);

clear3DSnapshotsButton.addEventListener("click", clear3DSnapshots);

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

window.addEventListener("resize", () => {
  drawInitialization();
  drawSelected3DPreview();
});

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
