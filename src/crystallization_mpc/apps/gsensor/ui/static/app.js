const statusText = document.querySelector("#status-text");
const initializedText = document.querySelector("#initialized-text");
const paramsBlock = document.querySelector("#params-block");
const messageBlock = document.querySelector("#message-block");
const shell = document.querySelector(".shell");
const toggleParametersButton = document.querySelector("#toggle-parameters");
const paramsForm = document.querySelector("#params-form");
const fieldTemplate = document.querySelector("#param-field-template");
const saveParamsButton = document.querySelector("#save-params");
const resetParamsButton = document.querySelector("#reset-params");
const parameterStatus = document.querySelector("#parameter-status");
const parameterStatusText = document.querySelector("#parameter-status-text");

const currentRunId = document.querySelector("#current-run-id");
const currentImageDirectory = document.querySelector("#current-image-directory");
const currentImageCount = document.querySelector("#current-image-count");
const imageSourceStatus = document.querySelector("#image-source-status");
const refreshImagesButton = document.querySelector("#refresh-images");
const imageScanStatus = document.querySelector("#image-scan-status");
const scanProcessedCount = document.querySelector("#scan-processed-count");
const scanPendingCount = document.querySelector("#scan-pending-count");
const scanLastImage = document.querySelector("#scan-last-image");
const scanFileModifiedAt = document.querySelector("#scan-file-modified-at");
const scanDetectedAt = document.querySelector("#scan-detected-at");
const scanLastError = document.querySelector("#scan-last-error");
const baselineStatus = document.querySelector("#baseline-status");
const baselineFrameSeq = document.querySelector("#baseline-frame-seq");
const baselineImage = document.querySelector("#baseline-image");
const baselineUDistance = document.querySelector("#baseline-u-distance");
const baselineVDistance = document.querySelector("#baseline-v-distance");
const baselineEstablishedAt = document.querySelector("#baseline-established-at");
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
const cornerReferenceInset = document.querySelector("#corner-reference-inset");
const cornerReferenceImage = document.querySelector("#corner-reference-image");
const cornerControls = document.querySelector("#corner-controls");
const candidateControlsWrap = document.querySelector("#candidate-controls-wrap");
const candidateControls = document.querySelector("#candidate-controls");
const confirm3DChoiceButton = document.querySelector("#confirm-3d-choice");
const runDscgrButton = document.querySelector("#run-dscgr");
const dscgrStatus = document.querySelector("#dscgr-status");
const pointList = document.querySelector("#point-list");
const initSaveStatus = document.querySelector("#init-save-status");
const measurementValidity = document.querySelector("#measurement-validity");
const measurementFrame = document.querySelector("#measurement-frame");
const measurementImage = document.querySelector("#measurement-image");
const measurementGU = document.querySelector("#measurement-g-u");
const measurementGUKf = document.querySelector("#measurement-g-u-kf");
const measurementGV = document.querySelector("#measurement-g-v");
const measurementGVKf = document.querySelector("#measurement-g-v-kf");
const measurementValidCount = document.querySelector("#measurement-valid-count");
const measurementInvalidCount = document.querySelector("#measurement-invalid-count");
const measurementPublishCount = document.querySelector("#measurement-publish-count");
const measurementInfluxCount = document.querySelector("#measurement-influx-count");
const measurementError = document.querySelector("#measurement-error");
const measurementOverlay = document.querySelector("#measurement-overlay");
const measurementOverlayCaption = document.querySelector("#measurement-overlay-caption");
const refreshOverlayButton = document.querySelector("#refresh-overlay");

const initContext = initCanvas.getContext("2d");
const threeDContext = threeDCanvas.getContext("2d");
const initImage = new Image();
const default3DView = { yaw: -0.65, pitch: 0.55 };

const state = {
  uiMode: "production",
  params: null,
  paramsVersion: 1,
  paramMeta: {},
  drawerOpen: false,
  parameterActionInFlight: false,
  parameterError: null,
  parameterUnsavedCount: 0,
  initialization: null,
  experimentSource: null,
  measurementActive: false,
  experimentLifecycleStatus: "not_started",
  sourceActionInFlight: false,
  liveStatusInFlight: false,
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
  cornerRequestInFlight: false,
  confirm3DChoiceInFlight: false,
  dscgrInFlight: false,
  latestOverlayFrame: null,
  lastOverlayRefreshAt: 0,
};

function applyUiMode(mode) {
  const resolved = mode === "development" ? "development" : "production";
  state.uiMode = resolved;
  document.documentElement.dataset.uiMode = resolved;
}

async function loadUiConfig() {
  applyUiMode("production");
  try {
    const payload = await fetchJson("/api/ui/config");
    applyUiMode(payload.mode);
    return payload;
  } catch (error) {
    return { mode: "production", development: false };
  }
}

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

function valuesEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function formatParameterTime(value) {
  if (!value) {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString();
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
      const modifiedBadge = field.querySelector(".field-modified");
      const resetButton = field.querySelector(".field-reset");
      const defaultValue = state.params?.defaults?.params?.[key];

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
      input.setAttribute("aria-label", meta.label || key);
      input.value = formatFieldValue(value);
      input.addEventListener("input", () => {
        state.parameterError = null;
        updateParameterDraftState();
      });
      resetButton.addEventListener("click", () => {
        input.value = formatFieldValue(defaultValue);
        state.parameterError = null;
        updateParameterDraftState();
        input.focus();
      });
      const modified = !valuesEqual(value, defaultValue);
      field.classList.toggle("modified", modified);
      modifiedBadge.hidden = !modified;
      resetButton.hidden = !modified;
      wrapper.appendChild(field);
    });

    paramsForm.appendChild(wrapper);
  });
}

function parametersLocked() {
  return [
    "waiting_for_initial_image",
    "initializing",
    "baseline_ready",
    "measuring",
    "stopping",
  ].includes(state.experimentLifecycleStatus);
}

function updateParameterDraftState() {
  if (!state.params) {
    return;
  }
  let unsavedCount = 0;
  let modifiedCount = 0;
  const locked = parametersLocked();
  paramsForm.querySelectorAll(".field-input").forEach((input) => {
    const key = input.dataset.key;
    const value = parseFieldValue(input.value);
    const savedValue = state.params?.params?.[key];
    const defaultValue = state.params?.defaults?.params?.[key];
    input.disabled = state.parameterActionInFlight || locked;
    if (!valuesEqual(value, savedValue)) {
      unsavedCount += 1;
    }
    const modified = !valuesEqual(value, defaultValue);
    if (modified) {
      modifiedCount += 1;
    }
    const field = input.closest(".field");
    field.classList.toggle("modified", modified);
    field.querySelector(".field-modified").hidden = !modified;
    const fieldReset = field.querySelector(".field-reset");
    fieldReset.hidden = !modified;
    fieldReset.disabled = state.parameterActionInFlight || locked;
  });

  saveParamsButton.disabled = state.parameterActionInFlight || locked || unsavedCount === 0;
  resetParamsButton.disabled = state.parameterActionInFlight || locked || modifiedCount === 0;
  state.parameterUnsavedCount = unsavedCount;
  renderParameterStatus(unsavedCount);
}

function renderParameterStatus(unsavedCount = 0) {
  let kind = state.params?.status?.kind || "loading";
  let message = state.params?.status?.message || "Loading parameters…";
  if (state.parameterActionInFlight) {
    kind = "saving";
    message = "Saving…";
  } else if (state.parameterError) {
    kind = "error";
    message = `Save/validation failed: ${state.parameterError}`;
  } else if (parametersLocked()) {
    kind = "applied";
    const runId = state.experimentSource?.run_id;
    message = `Applied${runId ? ` to ${runId}` : ""} · version ${state.params?.version || state.paramsVersion}`;
  } else if (unsavedCount > 0) {
    kind = "unsaved";
    message = `${unsavedCount} unsaved change${unsavedCount === 1 ? "" : "s"}`;
  } else if (kind === "draft_saved") {
    const savedTime = formatParameterTime(state.params?.status?.saved_at);
    if (savedTime) {
      message = `Draft saved at ${savedTime} · version ${state.params.version}`;
    }
  }
  parameterStatus.className = `parameter-status ${kind}`;
  parameterStatusText.textContent = message;
}

function collectForm() {
  const data = {};
  paramsForm.querySelectorAll(".field-input").forEach((input) => {
    data[input.dataset.key] = parseFieldValue(input.value);
  });
  return data;
}

function renderStatus(payload) {
  state.measurementActive = Boolean(payload.active);
  state.experimentLifecycleStatus = payload.experiment_lifecycle_status || "not_started";
  if (parametersLocked() && state.params && payload.params) {
    const paramsChanged = !valuesEqual(state.params.params, payload.params);
    state.params.params = payload.params;
    if (payload.experiment_parameter_version != null) {
      state.params.version = Number(payload.experiment_parameter_version);
      state.paramsVersion = state.params.version;
    }
    if (paramsChanged) {
      renderForm(state.params.params);
    }
  }
  updateParameterDraftState();
  statusText.textContent = state.experimentLifecycleStatus;
  const lifecycleRunning = [
    "waiting_for_initial_image",
    "initializing",
    "baseline_ready",
    "measuring",
  ].includes(state.experimentLifecycleStatus);
  statusText.className = state.experimentLifecycleStatus === "error"
    ? "status error"
    : (lifecycleRunning ? "status running" : "status idle");
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
    last_dscgr_result: payload.last_dscgr_result || null,
    experiment: payload.experiment || null,
    experiment_selection_status: payload.experiment_selection_status || null,
    experiment_selection_error: payload.experiment_selection_error || null,
    image_scan: payload.image_scan || null,
  }, null, 2);
  renderImageScan(payload.image_scan || {});
  renderBaseline(payload.baseline || null);
  renderOnlineMeasurement(payload);
  renderExperimentSource(state.experimentSource);
  renderInitialization(payload.initialization || null);
}

function formatGrowthRate(value, unit = "m/s") {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric.toExponential(6)} ${unit}` : "—";
}

function refreshMeasurementOverlay(frameSeq, { force = false, final = false } = {}) {
  if (!frameSeq) {
    return;
  }
  const now = Date.now();
  if (!force && (frameSeq === state.latestOverlayFrame || now - state.lastOverlayRefreshAt < 3000)) {
    return;
  }
  const kind = final ? "final" : "latest";
  measurementOverlay.src = `/api/measurement/overlay/${kind}?frame_seq=${encodeURIComponent(frameSeq)}&_=${now}`;
  measurementOverlay.hidden = false;
  measurementOverlayCaption.textContent = final
    ? `Final overlay · frame ${frameSeq}`
    : `Latest overlay · frame ${frameSeq}`;
  state.latestOverlayFrame = frameSeq;
  state.lastOverlayRefreshAt = now;
}

function renderOnlineMeasurement(payload) {
  const processing = payload.growth_rate_processing || {};
  const result = processing.latest_result || null;
  const publishing = payload.sample_publishing || {};
  const influx = payload.influx_persistence || {};
  const valid = Boolean(result?.valid);
  const hasResult = Boolean(result);

  measurementValidity.textContent = hasResult ? (valid ? "valid" : "invalid") : "no sample";
  measurementValidity.className = hasResult
    ? (valid ? "status running" : "status error")
    : "status idle";
  measurementFrame.textContent = hasResult ? String(result.frame_seq) : "—";
  measurementImage.textContent = result?.image_name || "—";
  measurementGU.textContent = formatGrowthRate(result?.u?.G, result?.unit);
  measurementGUKf.textContent = formatGrowthRate(result?.u?.G_KF, result?.unit);
  measurementGV.textContent = formatGrowthRate(result?.v?.G, result?.unit);
  measurementGVKf.textContent = formatGrowthRate(result?.v?.G_KF, result?.unit);
  measurementValidCount.textContent = String(processing.valid_frame_count || 0);
  measurementInvalidCount.textContent = String(processing.invalid_frame_count || 0);
  measurementPublishCount.textContent = String(publishing.success_count || 0);
  measurementInfluxCount.textContent = String(influx.success_count || 0);

  const errors = [result?.error, publishing.last_error, influx.last_error].filter(Boolean);
  measurementError.textContent = errors.length
    ? errors.join(" · ")
    : (hasResult ? `Processed at ${result.processed_at}` : "Waiting for the first post-baseline image.");
  measurementError.classList.toggle("error", errors.length > 0);

  const final = ["stopped", "completed"].includes(payload.experiment_lifecycle_status)
    && Boolean(processing.final_overlay_path);
  if (hasResult) {
    refreshMeasurementOverlay(result.frame_seq, { final });
  }
}

function renderBaseline(baseline) {
  const ready = Boolean(baseline && baseline.frame_seq === 0);
  baselineStatus.textContent = ready ? "ready" : "not ready";
  baselineStatus.className = ready ? "status running" : "status idle";
  baselineFrameSeq.textContent = ready ? String(baseline.frame_seq) : "—";
  baselineImage.textContent = ready ? baseline.image_name : "—";
  baselineUDistance.textContent = ready ? `${baseline.u?.distance_px ?? 0} px` : "—";
  baselineVDistance.textContent = ready ? `${baseline.v?.distance_px ?? 0} px` : "—";
  baselineEstablishedAt.textContent = ready ? baseline.established_at : "—";
}

function renderImageScan(scan) {
  const scanStatus = scan.status || "stopped";
  imageScanStatus.textContent = scanStatus;
  imageScanStatus.className = scanStatus === "error" ? "status error" : (
    ["running", "waiting_for_image"].includes(scanStatus) ? "status running" : "status idle"
  );
  scanProcessedCount.textContent = String(scan.processed_count || 0);
  scanPendingCount.textContent = String(scan.pending_image_count || 0);
  scanLastImage.textContent = scan.last_detected_image || "—";
  scanFileModifiedAt.textContent = scan.file_modified_at || "—";
  scanDetectedAt.textContent = scan.detected_at || "—";
  scanLastError.textContent = scan.error || "";
  scanLastError.hidden = !scan.error;
}

function renderExperimentSource(payload) {
  state.experimentSource = payload;
  const selected = Boolean(payload?.selected);
  const imageCount = Number(payload?.image_count || 0);
  currentRunId.textContent = selected ? payload.run_id : "No experiment selected";
  currentImageDirectory.textContent = selected ? payload.container_image_path : "—";
  currentImageCount.textContent = String(imageCount);

  refreshImagesButton.disabled = state.sourceActionInFlight;

  if (!selected) {
    imageSourceStatus.textContent = "Create or select an experiment in Central first.";
  } else if (state.experimentLifecycleStatus === "waiting_for_initial_image") {
    imageSourceStatus.textContent = "Waiting for the first readable camera image. Initialization will open automatically.";
  } else if (state.experimentLifecycleStatus === "initializing") {
    imageSourceStatus.textContent = "The first image is selected as frame 0. Complete the marking steps below.";
  } else if (["baseline_ready", "measuring"].includes(state.experimentLifecycleStatus)) {
    imageSourceStatus.textContent = "Frame 0 is ready. The directory is being scanned for later images.";
  } else if (imageCount === 0) {
    imageSourceStatus.textContent = "Start the experiment in Central; Gsensor will wait for the first camera image.";
  } else {
    imageSourceStatus.textContent = `${imageCount} images available. Start the experiment in Central to choose the baseline automatically.`;
  }
}

async function loadStatus() {
  const payload = await fetchJson("/api/status");
  renderStatus(payload);
  return payload;
}

async function loadExperimentSource() {
  const payload = await fetchJson("/api/initialization/source");
  renderExperimentSource(payload);
  return payload;
}

async function refreshOverview() {
  await Promise.all([loadStatus(), loadExperimentSource()]);
}

async function refreshLiveStatus() {
  if (state.liveStatusInFlight || document.hidden) {
    return;
  }
  state.liveStatusInFlight = true;
  try {
    const payload = await loadStatus();
    const statusRunId = payload.current_run_id || payload.experiment?.run_id || null;
    const displayedRunId = state.experimentSource?.run_id || null;
    if (statusRunId !== displayedRunId) {
      await loadExperimentSource();
    }
  } catch (error) {
    statusText.textContent = error.message;
    statusText.className = "status error";
  } finally {
    state.liveStatusInFlight = false;
  }
}

async function loadParams() {
  state.params = await fetchJson("/api/params");
  state.paramsVersion = state.params.version || 1;
  state.paramMeta = state.params.meta || {};
  state.parameterError = null;
  renderForm(state.params.params || {});
  updateParameterDraftState();
  return state.params;
}

async function saveParams() {
  state.parameterActionInFlight = true;
  state.parameterError = null;
  updateParameterDraftState();
  try {
    state.params = await fetchJson("/api/params", {
      method: "POST",
      body: JSON.stringify({
        version: state.paramsVersion,
        params: collectForm(),
      }),
    });
    state.paramsVersion = state.params.version || state.paramsVersion;
    state.paramMeta = state.params.meta || state.paramMeta;
    renderForm(state.params.params || {});
    await loadStatus();
  } catch (error) {
    state.parameterError = error.message;
  } finally {
    state.parameterActionInFlight = false;
    updateParameterDraftState();
  }
}

function renderInitialization(payload) {
  ensure3DSnapshotScope(payload);
  state.initialization = payload;
  const hasSession = Boolean(payload?.session_id);
  const step = payload?.current_step || null;
  const selected3DChoice = payload?.selected_3d_choice;
  const initializationEditable = state.experimentLifecycleStatus === "initializing";
  initStepTitle.textContent = hasSession ? payload.status : "Not started";
  initStepPrompt.textContent = step?.prompt || (selected3DChoice
    ? (initializationEditable
      ? `Previewing 3D choice ${selected3DChoice}. Compare other candidates or confirm this selection.`
      : `Confirmed 3D choice ${selected3DChoice}.`)
    : (hasSession ? "Waiting for the next initialization step." : "Start the experiment in Central. Marking opens when the first image arrives."));
  initSaveStatus.textContent = hasSession
    ? `${payload.selected_image || ""}`
    : "idle";

  undoInitButton.disabled = !initializationEditable || !payload?.can_undo;
  resetInitButton.disabled = !initializationEditable || !hasSession;
  confirm3DChoiceButton.disabled = !initializationEditable
    || payload?.status !== "ready_for_3d"
    || selected3DChoice == null
    || state.confirm3DChoiceInFlight;
  runDscgrButton.disabled = payload?.status !== "ready_for_3d" || state.dscgrInFlight;

  fullModeControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !initializationEditable || !hasSession || step?.key !== "is_full";
    const selected = String(payload?.is_full) === button.dataset.fullMode;
    button.classList.toggle("selected", selected);
  });

  renderCandidateControls(payload);
  renderPointList(payload);
  renderCornerControls(payload);
  renderCornerReferenceInset(payload);
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
    button.disabled = state.experimentLifecycleStatus !== "initializing"
      || !["ready_for_3d_choice", "ready_for_3d"].includes(payload?.status);
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

function canCompareCorner(payload) {
  return Boolean(
    payload?.session_id
    && ["ready_for_corner", "ready_for_3d_choice", "ready_for_3d"].includes(payload?.status)
  );
}

function activeCornerReference(payload) {
  if (!canCompareCorner(payload)) {
    return null;
  }
  return payload?.corner || null;
}

function renderCornerControls(payload) {
  const enabled = state.experimentLifecycleStatus === "initializing"
    && canCompareCorner(payload)
    && !state.cornerRequestInFlight;
  const activeCorner = activeCornerReference(payload);
  cornerControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !enabled;
    button.classList.toggle("selected", activeCorner === button.dataset.corner);
  });
}

function renderCornerReferenceInset(payload) {
  const corner = activeCornerReference(payload);
  if (!corner || !state.imageReady) {
    cornerReferenceInset.hidden = true;
    cornerReferenceImage.removeAttribute("src");
    return;
  }

  cornerReferenceImage.src = `/static/imgs/corner_${corner}.jpg`;
  cornerReferenceImage.alt = `Corner ${corner}`;
  cornerReferenceInset.hidden = false;
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
  renderCornerReferenceInset(state.initialization);
  drawSelected3DPreview();
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
  const imagePlane = loadedImagePlane();
  const imageTransformed = imagePlane
    ? imagePlane.vertices.map((vertex) => transform3DVertex(vertex))
    : [];
  const projected = project3DVertices(
    [...transformed, ...imageTransformed],
    threeDCanvas.width,
    threeDCanvas.height,
  );
  const projectedPatchPoints = projected.points.slice(0, transformed.length);
  const projectedImagePoints = projected.points.slice(transformed.length);
  const alpha = Math.max(0, Math.min(Number(patch.face_alpha) || 0.16, 1));
  const sortedFaces = faces.slice().sort((left, right) => averageFaceZ(transformed, right) - averageFaceZ(transformed, left));

  threeDContext.clearRect(0, 0, threeDCanvas.width, threeDCanvas.height);
  drawLoadedImagePlane(projectedImagePoints, imagePlane);
  draw3DAxes(projected.scale, threeDCanvas.width, threeDCanvas.height);
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

function loadedImagePlane() {
  if (!state.imageReady || !initImage.naturalWidth || !initImage.naturalHeight) {
    return null;
  }
  const width = initImage.naturalWidth;
  const height = initImage.naturalHeight;
  return {
    width,
    height,
    vertices: [
      [0, 0, 0],
      [width, 0, 0],
      [width, height, 0],
      [0, height, 0],
    ],
  };
}

function drawLoadedImagePlane(points, imagePlane) {
  if (!imagePlane || points.length < 4) {
    return;
  }
  const [topLeft, topRight, _bottomRight, bottomLeft] = points;
  if (![topLeft, topRight, bottomLeft].every((point) => (
    Number.isFinite(point?.x) && Number.isFinite(point?.y)
  ))) {
    return;
  }
  threeDContext.save();
  threeDContext.imageSmoothingEnabled = true;
  threeDContext.imageSmoothingQuality = "high";
  threeDContext.setTransform(
    (topRight.x - topLeft.x) / imagePlane.width,
    (topRight.y - topLeft.y) / imagePlane.width,
    (bottomLeft.x - topLeft.x) / imagePlane.height,
    (bottomLeft.y - topLeft.y) / imagePlane.height,
    topLeft.x,
    topLeft.y,
  );
  threeDContext.drawImage(initImage, 0, 0, imagePlane.width, imagePlane.height);
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

async function runDscgr() {
  if (!state.initialization?.session_id || state.initialization.status !== "ready_for_3d") {
    return;
  }
  state.dscgrInFlight = true;
  runDscgrButton.disabled = true;
  dscgrStatus.textContent = "running";
  try {
    const result = await fetchJson("/api/dscgr/run", {
      method: "POST",
      body: JSON.stringify({ session_id: state.initialization.session_id }),
    });
    const count = Array.isArray(result.processed_ptrs) ? result.processed_ptrs.length : 0;
    dscgrStatus.textContent = `done: ${count} frames, ${result.output_dir || ""}`;
    await loadStatus();
  } catch (error) {
    dscgrStatus.textContent = error.message;
  } finally {
    state.dscgrInFlight = false;
    renderInitialization(state.initialization);
  }
}

toggleParametersButton.addEventListener("click", () => {
  setDrawerOpen(!state.drawerOpen);
});

saveParamsButton.addEventListener("click", async () => {
  await saveParams();
});

resetParamsButton.addEventListener("click", async () => {
  state.parameterActionInFlight = true;
  state.parameterError = null;
  updateParameterDraftState();
  try {
    state.params = await fetchJson("/api/params/reset", {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.paramsVersion = state.params.version || 1;
    state.paramMeta = state.params.meta || {};
    renderForm(state.params.params || {});
    await loadStatus();
  } catch (error) {
    state.parameterError = error.message;
  } finally {
    state.parameterActionInFlight = false;
    updateParameterDraftState();
  }
});

refreshImagesButton.addEventListener("click", async () => {
  state.sourceActionInFlight = true;
  renderExperimentSource(state.experimentSource);
  try {
    await loadExperimentSource();
  } catch (error) {
    imageSourceStatus.textContent = error.message;
  } finally {
    state.sourceActionInFlight = false;
    renderExperimentSource(state.experimentSource);
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
  const selectedCorner = button.dataset.corner;
  state.cornerRequestInFlight = true;
  renderCornerControls(state.initialization);
  try {
    const payload = await fetchJson("/api/initialization/corner", {
      method: "POST",
      body: JSON.stringify({
        session_id: state.initialization.session_id,
        corner: selectedCorner,
      }),
    });
    renderInitialization(payload);
  } catch (error) {
    initSaveStatus.textContent = error.message;
  } finally {
    state.cornerRequestInFlight = false;
    renderCornerControls(state.initialization);
    renderCornerReferenceInset(state.initialization);
  }
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

confirm3DChoiceButton.addEventListener("click", async () => {
  if (!state.initialization?.session_id
      || state.initialization?.selected_3d_choice == null
      || state.initialization?.status !== "ready_for_3d") {
    return;
  }
  state.confirm3DChoiceInFlight = true;
  initSaveStatus.textContent = "confirming selected 3D candidate";
  renderInitialization(state.initialization);
  try {
    captureCurrent3DSnapshot();
    const payload = await fetchJson("/api/initialization/confirm", {
      method: "POST",
      body: JSON.stringify({ session_id: state.initialization.session_id }),
    });
    renderInitialization(payload);
    initSaveStatus.textContent = "baseline established; online measurement started";
    await loadStatus();
  } catch (error) {
    initSaveStatus.textContent = error.message;
  } finally {
    state.confirm3DChoiceInFlight = false;
    renderInitialization(state.initialization);
  }
});

runDscgrButton.addEventListener("click", async () => {
  await runDscgr();
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

refreshOverlayButton.addEventListener("click", () => {
  const frameSeq = Number(measurementFrame.textContent);
  if (Number.isFinite(frameSeq) && frameSeq > 0) {
    refreshMeasurementOverlay(frameSeq, {
      force: true,
      final: ["stopped", "completed"].includes(state.experimentLifecycleStatus),
    });
  }
});

measurementOverlay.addEventListener("error", () => {
  measurementOverlay.hidden = true;
  measurementOverlayCaption.textContent = "Overlay is not available yet.";
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

window.addEventListener("resize", () => {
  drawInitialization();
  renderCornerReferenceInset(state.initialization);
  drawSelected3DPreview();
});

Promise.all([loadUiConfig(), loadParams(), refreshOverview()]).catch((error) => {
  statusText.textContent = error.message;
  statusText.className = "status error";
  state.parameterError = error.message;
  updateParameterDraftState();
});

window.addEventListener("focus", () => {
  if (state.parameterActionInFlight) {
    return;
  }
  const parameterRefresh = state.parameterUnsavedCount === 0 ? loadParams() : Promise.resolve();
  Promise.all([parameterRefresh, refreshOverview()]).catch((error) => {
    statusText.textContent = error.message;
    statusText.className = "status error";
  });
});

document.addEventListener("visibilitychange", () => {
  if (!state.parameterActionInFlight && !document.hidden) {
    const parameterRefresh = state.parameterUnsavedCount === 0 ? loadParams() : Promise.resolve();
    Promise.all([parameterRefresh, refreshOverview()]).catch((error) => {
      statusText.textContent = error.message;
      statusText.className = "status error";
    });
  }
});

window.setInterval(refreshLiveStatus, 1000);
