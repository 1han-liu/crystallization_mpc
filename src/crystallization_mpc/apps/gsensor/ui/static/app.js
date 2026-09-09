const statusText = document.querySelector("#status-text");
const initializedText = document.querySelector("#initialized-text");
const connectionStatus = document.querySelector("#connection-status");
const paramsBlock = document.querySelector("#params-block");
const messageBlock = document.querySelector("#message-block");
const runParametersButton = document.querySelector("#run-parameters-button");
const runParametersDialog = document.querySelector("#run-parameters-dialog");
const closeRunParametersButton = document.querySelector("#close-run-parameters");
const parameterRunLabel = document.querySelector("#parameter-run-label");
const parameterRunId = document.querySelector("#parameter-run-id");
const parameterRunVersion = document.querySelector("#parameter-run-version");
const runParametersStatus = document.querySelector("#run-parameters-status");
const runParametersValues = document.querySelector("#run-parameters-values");
const alignmentMethodSelect = document.querySelector("#alignment-method-select");
const alignmentConfigurationStatus = document.querySelector("#alignment-configuration-status");
const alignmentSelectionHelp = document.querySelector("#alignment-selection-help");
const alignmentCapabilityNotes = document.querySelector("#alignment-capability-notes");
let parameterDialogOpener = null;

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
const alignmentMethod = document.querySelector("#alignment-method");
const alignmentStatus = document.querySelector("#alignment-status");
const alignmentTranslation = document.querySelector("#alignment-translation");
const alignmentRotation = document.querySelector("#alignment-rotation");
const alignmentRuntime = document.querySelector("#alignment-runtime");
const measurementPublishCount = document.querySelector("#measurement-publish-count");
const measurementInfluxCount = document.querySelector("#measurement-influx-count");
const measurementError = document.querySelector("#measurement-error");
const measurementOverlay = document.querySelector("#measurement-overlay");
const measurementOverlayCaption = document.querySelector("#measurement-overlay-caption");
const refreshOverlayButton = document.querySelector("#refresh-overlay");

const initContext = initCanvas.getContext("2d");
const threeDContext = threeDCanvas.getContext("2d");
let initImage = null;
const default3DView = { yaw: -0.65, pitch: 0.55 };

const state = {
  uiMode: "production",
  paramMeta: {},
  experimentParameters: null,
  parameterRenderKey: null,
  alignmentConfiguration: null,
  alignmentDraft: null,
  alignmentDraftScope: null,
  alignmentDraftEdited: false,
  alignmentCapabilitiesReady: false,
  alignmentCapabilitiesError: null,
  initialized: false,
  initialization: null,
  experimentSource: null,
  measurementActive: false,
  experimentLifecycleStatus: "not_started",
  sourceActionInFlight: false,
  liveStatusInFlight: false,
  statusAvailable: false,
  runId: undefined,
  contextRevision: 0,
  statusRequest: 0,
  sourceRequest: 0,
  paramsRequest: 0,
  initializationRevision: 0,
  initializationAction: null,
  initializationFeedback: null,
  confirmedSessionId: null,
  sourceError: null,
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
  dscgrInFlight: false,
  latestOverlayFrame: null,
  lastOverlayRefreshAt: 0,
  overlayRequest: 0,
  overlayImage: null,
  alignmentCapabilities: {},
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

function cacheBustedUrl(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET" || !url.startsWith("/api/")) {
    return url;
  }
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}_=${Date.now()}`;
}

async function fetchJson(url, options = {}) {
  let response;
  try {
    response = await fetch(cacheBustedUrl(url, options), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      ...options,
    });
  } catch (error) {
    error.outcomeUnknown = true;
    throw error;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg || String(item)).join("; ") : detail;
    throw new Error(message || `Request failed (${response.status})`);
  }
  if (payload === null) {
    const error = new Error("The server response could not be read. Refresh status before trying again.");
    error.outcomeUnknown = true;
    throw error;
  }
  return payload;
}

function clearMeasurementOverlay() {
  state.overlayRequest += 1;
  state.overlayImage = null;
  state.latestOverlayFrame = null;
  state.lastOverlayRefreshAt = 0;
  measurementOverlay.hidden = true;
  measurementOverlay.removeAttribute("src");
  measurementOverlayCaption.textContent = "No overlay for the current experiment.";
  refreshOverlayButton.disabled = true;
}

function setStatusAvailable(available, message = "") {
  state.statusAvailable = available;
  connectionStatus.hidden = available;
  connectionStatus.textContent = available ? "" : `Status connection lost. Measurements, images and parameters may be outdated. Editing is disabled until status refresh succeeds. ${message}`;
  renderRunParameters();
  renderInitialization(state.initialization);
  refreshOverlayButton.disabled = !available || !state.latestOverlayFrame;
}

function adoptExperiment(payload) {
  const runId = payload.current_run_id || payload.experiment?.run_id || null;
  if (state.runId === runId) {
    return;
  }
  state.runId = runId;
  state.contextRevision += 1;
  state.initializationRevision += 1;
  state.initializationAction = null;
  state.initializationFeedback = null;
  state.confirmedSessionId = null;
  state.experimentParameters = null;
  state.alignmentConfiguration = null;
  state.alignmentDraft = null;
  state.alignmentDraftScope = null;
  state.alignmentDraftEdited = false;
  state.initialized = false;
  state.sourceError = null;
  clearMeasurementOverlay();
  renderInitialization(null);
  renderExperimentSource({
    ...(payload.experiment || {}), selected: Boolean(runId), run_id: runId, image_count: 0,
  });
}

function renderStatus(payload, { includeInitialization = true } = {}) {
  adoptExperiment(payload);
  state.statusAvailable = true;
  connectionStatus.hidden = true;
  state.measurementActive = Boolean(payload.active);
  state.experimentLifecycleStatus = payload.experiment_lifecycle_status || "not_started";
  state.initialized = Boolean(payload.initialized);
  state.alignmentConfiguration = payload.alignment_configuration || null;
  const snapshot = payload.experiment_parameters;
  // Only the run snapshot is authoritative. /api/params can be a later Central draft.
  state.experimentParameters = snapshot?.run_id === state.runId ? snapshot : null;
  renderRunParameters();
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
  if (includeInitialization || payload.initialization?.session_id !== state.initialization?.session_id) {
    if (payload.initialization?.session_id !== state.initialization?.session_id && state.initializationAction) {
      state.initializationAction = null;
      state.initializationRevision += 1;
    }
    renderInitialization(payload.initialization || null);
  } else {
    renderInitialization(state.initialization);
  }
}

function formatGrowthRate(value, unit = "m/s") {
  if (value == null || value === "") {
    return "—";
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric.toExponential(6)} ${unit}` : "—";
}

function refreshMeasurementOverlay(frameSeq, { force = false, final = false, processedAt = "" } = {}) {
  if (!frameSeq || !state.runId || !state.statusAvailable) {
    return;
  }
  const now = Date.now();
  const kind = final ? "final" : "latest";
  const key = `${state.runId}:${kind}:${frameSeq}:${processedAt}`;
  if (!force && (key === state.latestOverlayFrame || (state.latestOverlayFrame?.startsWith(`${state.runId}:${kind}:`) && now - state.lastOverlayRefreshAt < 3000))) {
    return;
  }
  const runId = state.runId;
  const revision = ++state.overlayRequest;
  const image = new Image();
  state.overlayImage = image;
  image.onload = () => {
    if (revision !== state.overlayRequest || runId !== state.runId) return;
    measurementOverlay.src = image.src;
    measurementOverlay.hidden = false;
    measurementOverlayCaption.textContent = `${final ? "Final" : "Latest"} overlay · ${runId} · frame ${frameSeq}`;
  };
  image.onerror = () => {
    if (revision !== state.overlayRequest || runId !== state.runId) return;
    measurementOverlay.hidden = true;
    measurementOverlay.removeAttribute("src");
    measurementOverlayCaption.textContent = "Overlay is not available yet. Refresh Preview to try again.";
    state.latestOverlayFrame = null;
  };
  image.src = `/api/measurement/overlay/${kind}?run_id=${encodeURIComponent(runId)}&frame_seq=${encodeURIComponent(frameSeq)}&_=${now}`;
  state.latestOverlayFrame = key;
  state.lastOverlayRefreshAt = now;
  refreshOverlayButton.disabled = false;
}

function renderOnlineMeasurement(payload) {
  const processing = payload.growth_rate_processing || {};
  const candidateResult = processing.latest_result || null;
  const result = candidateResult && (!candidateResult.run_id || candidateResult.run_id === state.runId) ? candidateResult : null;
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
  const alignment = result?.alignment || null;
  alignmentMethod.textContent = alignment?.method || "—";
  alignmentStatus.textContent = alignment
    ? (alignment.success ? (alignment.fallback_used ? "fallback" : "success") : "failed")
    : "—";
  alignmentTranslation.textContent = alignment
    ? `${Number(alignment.tx_px || 0).toFixed(2)}, ${Number(alignment.ty_px || 0).toFixed(2)} px`
    : "—";
  alignmentRotation.textContent = alignment
    ? `${Number(alignment.rotation_deg || 0).toFixed(3)}°`
    : "—";
  alignmentRuntime.textContent = alignment
    ? `${Number(alignment.runtime_ms || 0).toFixed(1)} ms`
    : "—";
  measurementPublishCount.textContent = String(publishing.success_count || 0);
  measurementInfluxCount.textContent = String(influx.success_count || 0);

  const errors = [
    result?.error,
    alignment?.error,
    publishing.last_error,
    influx.last_error,
  ].filter(Boolean);
  measurementError.textContent = errors.length
    ? errors.join(" · ")
    : (hasResult ? `Processed at ${result.processed_at}` : "Waiting for the first post-baseline image.");
  measurementError.classList.toggle("error", errors.length > 0);

  const final = ["stopped", "completed"].includes(payload.experiment_lifecycle_status)
    && Boolean(processing.final_overlay_path);
  if (hasResult) {
    refreshMeasurementOverlay(result.frame_seq, { final, processedAt: result.processed_at });
  } else {
    clearMeasurementOverlay();
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
  currentImageDirectory.textContent = selected ? (payload.container_image_path || "—") : "—";
  currentImageCount.textContent = String(imageCount);

  refreshImagesButton.disabled = state.sourceActionInFlight;

  imageSourceStatus.classList.toggle("error", Boolean(state.sourceError));
  if (state.sourceError) {
    imageSourceStatus.textContent = `Image list refresh failed: ${state.sourceError}`;
  } else if (!selected) {
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
  const request = ++state.statusRequest;
  const initializationRevision = state.initializationRevision;
  try {
    const payload = await fetchJson("/api/status");
    if (request !== state.statusRequest || initializationRevision !== state.initializationRevision) return null;
    renderStatus(payload, { includeInitialization: !state.initializationAction });
    return payload;
  } catch (error) {
    if (request !== state.statusRequest || initializationRevision !== state.initializationRevision) return null;
    setStatusAvailable(false, error.message);
    throw error;
  }
}

async function loadExperimentSource() {
  const request = ++state.sourceRequest;
  const context = state.contextRevision;
  let payload;
  try {
    payload = await fetchJson("/api/initialization/source");
  } catch (error) {
    if (request !== state.sourceRequest || context !== state.contextRevision) return null;
    state.sourceError = error.message;
    renderExperimentSource(state.experimentSource);
    throw error;
  }
  const runId = payload.selected ? payload.run_id : null;
  if (request !== state.sourceRequest || context !== state.contextRevision || runId !== state.runId) return null;
  state.sourceError = null;
  renderExperimentSource(payload);
  return payload;
}

async function refreshOverview() {
  const payload = await loadStatus();
  if (payload) await loadExperimentSource();
}

async function refreshLiveStatus() {
  if (state.liveStatusInFlight || document.hidden) {
    return;
  }
  state.liveStatusInFlight = true;
  try {
    const payload = await loadStatus();
    if (!payload) return;
    const statusRunId = payload.current_run_id || payload.experiment?.run_id || null;
    const displayedRunId = state.experimentSource?.run_id || null;
    if (statusRunId !== displayedRunId || (statusRunId && state.experimentSource?.image_count === 0)) {
      await loadExperimentSource().catch(() => {});
    }
  } catch (error) {
    statusText.textContent = error.message;
    statusText.className = "status error";
  } finally {
    state.liveStatusInFlight = false;
  }
}

async function loadParameterMetadata() {
  const request = ++state.paramsRequest;
  const results = await Promise.allSettled([
    fetchJson("/api/params"),
    fetchJson("/api/alignment/capabilities"),
  ]);
  if (request !== state.paramsRequest) return;
  const [metadata, capabilities] = results;
  if (metadata.status === "fulfilled") state.paramMeta = metadata.value.meta || {};
  if (capabilities.status === "fulfilled" && Array.isArray(capabilities.value.methods)) {
    state.alignmentCapabilities = Object.fromEntries(
      capabilities.value.methods.filter((item) => typeof item?.method === "string").map((item) => [item.method, item]),
    );
    state.alignmentCapabilitiesReady = Object.keys(state.alignmentCapabilities).length > 0;
    state.alignmentCapabilitiesError = state.alignmentCapabilitiesReady ? null : "No alignment capabilities were returned.";
  } else {
    state.alignmentCapabilitiesReady = false;
    state.alignmentCapabilitiesError = capabilities.reason?.message || "Alignment capabilities could not be loaded.";
  }
  renderInitialization(state.initialization);
  renderRunParameters();
}

function alignmentLabel(method) {
  if (method === "none") return "None";
  return state.alignmentCapabilities[method]?.label || String(method || "Unknown").replaceAll("_", " ").toUpperCase();
}

function syncAlignmentDraft() {
  const scope = `${state.runId || ""}:${state.initialization?.session_id || ""}`;
  const configuration = state.alignmentConfiguration;
  if (state.alignmentDraftScope !== scope) {
    state.alignmentDraftScope = scope;
    state.alignmentDraftEdited = false;
  }
  if (!state.alignmentDraftEdited) state.alignmentDraft = configuration?.method ?? configuration?.configured_method ?? "none";
  if (configuration?.confirmed) state.alignmentDraft = configuration.method;
}

function alignmentSelectable() {
  return initializationEditable() && Boolean(state.initialization?.session_id)
    && state.alignmentConfiguration?.can_select === true
    && !state.alignmentConfiguration?.confirmed
    && state.alignmentCapabilitiesReady;
}

function alignmentSelectionReady() {
  // During a rolling upgrade the previous service can still confirm initialization.
  if (!state.alignmentConfiguration) return true;
  return alignmentSelectable() && state.alignmentCapabilities[state.alignmentDraft]?.available === true;
}

function renderAlignmentConfiguration() {
  syncAlignmentDraft();
  const configuration = state.alignmentConfiguration;
  const method = state.alignmentDraft || "none";
  const methods = new Set(["none", ...Object.keys(state.alignmentCapabilities), method]);
  const optionsKey = JSON.stringify([...methods].map((value) => [value, state.alignmentCapabilities[value]]));
  if (alignmentMethodSelect.dataset.optionsKey !== optionsKey) {
    alignmentMethodSelect.replaceChildren();
    methods.forEach((value) => {
      const option = document.createElement("option");
      const capability = state.alignmentCapabilities[value];
      option.value = value;
      option.textContent = value === "none" ? "None — no image alignment" : alignmentLabel(value);
      option.disabled = capability?.available !== true;
      if (capability?.available === false) option.textContent += " (unavailable)";
      option.title = capability?.reason || "";
      alignmentMethodSelect.appendChild(option);
    });
    alignmentMethodSelect.dataset.optionsKey = optionsKey;
  }
  alignmentMethodSelect.value = method;
  alignmentMethodSelect.disabled = !alignmentSelectable();
  const confirmed = Boolean(configuration?.confirmed);
  alignmentConfigurationStatus.textContent = confirmed ? `confirmed · ${alignmentLabel(configuration.method)}`
    : (state.initialization?.session_id ? `draft · ${alignmentLabel(method)}` : "waiting for initialization");
  alignmentConfigurationStatus.className = confirmed ? "status running" : "status idle";
  let help;
  if (!state.statusAvailable) {
    help = "Status is unavailable. Alignment selection is disabled until the connection recovers.";
  } else if (confirmed) {
    help = `${alignmentLabel(configuration.method)} was confirmed with the selected 3D candidate and is locked for this experiment.`;
  } else if (state.initializationAction) {
    help = "Waiting for the initialization operation to finish. Your alignment draft is retained.";
  } else if (state.confirmedSessionId === state.initialization?.session_id && state.confirmedSessionId) {
    help = "Candidate confirmation was accepted. Waiting for the confirmed alignment status; do not submit again.";
  } else if (!state.initialization?.session_id) {
    help = "Start an experiment in Central. Choose an alignment method during image marking.";
  } else if (!configuration) {
    help = "This service has not provided alignment configuration. Image marking remains available.";
    alignmentConfigurationStatus.textContent = "configuration unavailable";
  } else if (!configuration.can_select || state.initialized) {
    help = "Alignment can be selected only during manual initialization, before confirming the candidate.";
    alignmentConfigurationStatus.textContent = "selection locked";
  } else if (!state.alignmentCapabilitiesReady) {
    help = "Alignment capabilities are unavailable. Use Refresh Images to retry before confirming.";
  } else if (state.alignmentCapabilities[method]?.available !== true) {
    help = "This method is unavailable. Choose an available method before confirming the candidate.";
  } else {
    help = `Draft: ${alignmentLabel(method)}. “Confirm selected candidate” also confirms this method and locks it for this experiment. None skips image alignment.`;
  }
  alignmentSelectionHelp.textContent = help;
  const unavailable = Object.values(state.alignmentCapabilities)
    .filter((item) => item.available === false)
    .map((item) => `${alignmentLabel(item.method)}: ${item.reason || "unavailable on this service"}`);
  alignmentCapabilityNotes.textContent = state.alignmentCapabilitiesError || unavailable.join(" · ");
  alignmentCapabilityNotes.hidden = !alignmentCapabilityNotes.textContent || confirmed;
}

function renderRunParameters() {
  const snapshot = state.experimentParameters;
  parameterRunLabel.textContent = snapshot?.label || "—";
  parameterRunId.textContent = snapshot?.run_id || state.runId || "—";
  parameterRunVersion.textContent = snapshot?.parameter_version ?? "—";
  const configuration = snapshot?.alignment_configuration;
  const alignmentMessage = configuration?.confirmed
    ? `Confirmed alignment: ${alignmentLabel(configuration.method)}.`
    : "Alignment has not been confirmed. The selection in Image Alignment is a local draft until candidate confirmation.";
  runParametersStatus.textContent = !snapshot
    ? (state.runId ? "No run parameter snapshot is available. Parameters become available after Start in Central."
      : "No experiment has started. Parameters become available after Start in Central.")
    : `Effective run parameters · ${alignmentMessage}`;
  if (!state.statusAvailable) runParametersStatus.textContent = `Status unavailable; displayed values may be outdated. ${runParametersStatus.textContent}`;
  const renderKey = JSON.stringify([snapshot?.params || {}, state.paramMeta]);
  if (renderKey === state.parameterRenderKey) return;
  state.parameterRenderKey = renderKey;
  runParametersValues.replaceChildren();
  const entries = Object.entries(snapshot?.params || {}).sort(([left], [right]) => {
    const leftOrder = state.paramMeta[left]?.ui?.order ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = state.paramMeta[right]?.ui?.order ?? Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || left.localeCompare(right);
  });
  const groups = new Map();
  entries.forEach(([key, value]) => {
    const meta = state.paramMeta[key] || {};
    const section = meta.section || "Parameters";
    if (!groups.has(section)) {
      const group = document.createElement("section");
      group.className = "run-parameter-group";
      const heading = document.createElement("h3");
      heading.textContent = section;
      const list = document.createElement("dl");
      list.className = "run-parameter-list";
      group.append(heading, list);
      groups.set(section, list);
      runParametersValues.appendChild(group);
    }
    const row = document.createElement("div");
    const label = document.createElement("dt");
    label.textContent = `${meta.label || key}${meta.unit ? ` (${meta.unit})` : ""}`;
    if (meta.label && meta.label !== key) {
      const parameterKey = document.createElement("code");
      parameterKey.textContent = key;
      label.appendChild(parameterKey);
    }
    const content = document.createElement("dd");
    content.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    row.append(label, content);
    groups.get(section).appendChild(row);
  });
}

function initializationEditable() {
  return state.statusAvailable && state.experimentLifecycleStatus === "initializing"
    && !state.initialized && !state.alignmentConfiguration?.confirmed
    && !state.initializationAction && !state.dscgrInFlight
    && (!state.confirmedSessionId || state.confirmedSessionId !== state.initialization?.session_id);
}

function renderInitialization(payload) {
  if (state.initialization?.session_id !== payload?.session_id) {
    state.initializationFeedback = null;
    state.confirmedSessionId = null;
  }
  ensure3DSnapshotScope(payload);
  state.initialization = payload;
  renderAlignmentConfiguration();
  const hasSession = Boolean(payload?.session_id);
  const step = payload?.current_step || null;
  const selected3DChoice = payload?.selected_3d_choice;
  const editable = initializationEditable();
  initStepTitle.textContent = hasSession ? payload.status : "Not started";
  initStepPrompt.textContent = step?.prompt || (selected3DChoice
    ? (state.experimentLifecycleStatus === "initializing"
      ? `Previewing 3D choice ${selected3DChoice}. Compare other candidates or confirm this selection.`
      : `Confirmed 3D choice ${selected3DChoice}.`)
    : (hasSession ? "Waiting for the next initialization step." : "Start the experiment in Central. Marking opens when the first image arrives."));
  const feedback = state.initializationFeedback;
  initSaveStatus.textContent = feedback?.message || (hasSession ? `${payload.selected_image || ""}` : "idle");
  initSaveStatus.classList.toggle("error", feedback?.kind === "error");
  initSaveStatus.classList.toggle("pending", Boolean(state.initializationAction));
  initCanvas.setAttribute("aria-busy", String(Boolean(state.initializationAction)));
  initCanvas.setAttribute("aria-disabled", String(!editable || !state.imageReady || step?.type !== "point"));

  undoInitButton.disabled = !editable || !payload?.can_undo;
  resetInitButton.disabled = !editable || !hasSession;
  confirm3DChoiceButton.disabled = !editable
    || !alignmentSelectionReady()
    || payload?.status !== "ready_for_3d"
    || selected3DChoice == null;
  runDscgrButton.disabled = !state.statusAvailable || payload?.status !== "ready_for_3d" || state.dscgrInFlight || Boolean(state.initializationAction);

  fullModeControls.querySelectorAll("button").forEach((button) => {
    button.disabled = !editable || !hasSession || step?.key !== "is_full";
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
    button.disabled = !initializationEditable()
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
  const enabled = initializationEditable()
    && canCompareCorner(payload);
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
    initImage = null;
    state.imageReady = false;
    initCanvas.width = 0;
    initCanvas.height = 0;
    canvasEmpty.hidden = false;
    canvasEmpty.textContent = "No image loaded";
    return;
  }
  const imageKey = `${state.runId}:${payload.session_id}:${payload.selected_image}`;
  if (state.currentImageKey === imageKey) {
    return;
  }
  state.currentImageKey = imageKey;
  state.imageReady = false;
  initCanvas.width = 0;
  initCanvas.height = 0;
  canvasEmpty.hidden = false;
  canvasEmpty.textContent = "Loading initialization image…";
  const image = new Image();
  initImage = image;
  image.onload = () => {
    if (state.currentImageKey !== imageKey || initImage !== image) return;
    state.imageReady = true;
    initCanvas.width = image.naturalWidth;
    initCanvas.height = image.naturalHeight;
    canvasEmpty.hidden = true;
    renderInitialization(state.initialization);
  };
  image.onerror = () => {
    if (state.currentImageKey !== imageKey || initImage !== image) return;
    state.imageReady = false;
    canvasEmpty.hidden = false;
    canvasEmpty.textContent = "Image preview could not be loaded. Refresh Images to try again.";
    initCanvas.setAttribute("aria-disabled", "true");
  };
  image.src = `/api/initialization/image/${encodeURIComponent(payload.session_id)}?_=${Date.now()}`;
}

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
  if (!initializationEditable() || !state.imageReady || !payload?.session_id || step?.type !== "point") {
    return;
  }
  const rect = initCanvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) * (initCanvas.width / rect.width);
  const y = (event.clientY - rect.top) * (initCanvas.height / rect.height);
  await performInitializationAction("point", { x, y }, `Saving ${step.label || step.key}…`, "Point saved.");
}

async function performInitializationAction(endpoint, fields, pendingMessage, successMessage, { refresh = false } = {}) {
  if (!initializationEditable() || !state.initialization?.session_id) return;
  const action = { context: state.contextRevision, sessionId: state.initialization.session_id };
  state.initializationAction = action;
  state.initializationRevision += 1;
  state.initializationFeedback = { kind: "pending", message: pendingMessage };
  renderInitialization(state.initialization);
  const current = () => state.initializationAction === action && state.contextRevision === action.context
    && state.initialization?.session_id === action.sessionId;
  try {
    const payload = await fetchJson(`/api/initialization/${endpoint}`, {
      method: "POST", body: JSON.stringify({ session_id: action.sessionId, ...fields }),
    });
    if (!current()) return;
    state.initializationRevision += 1;
    renderInitialization(payload);
    if (endpoint === "confirm") state.confirmedSessionId = action.sessionId;
    state.initializationFeedback = { kind: "success", message: successMessage };
    if (refresh) {
      try {
        await loadStatus();
      } catch (error) {
        if (state.contextRevision === action.context) {
          state.initializationFeedback = { kind: "error", message: `${successMessage} Status refresh failed; the action succeeded. Wait for status to reconnect instead of submitting again.` };
        }
      }
    }
  } catch (error) {
    if (current()) {
      state.initializationFeedback = { kind: "error", message: error.outcomeUnknown
        ? `Request outcome is unknown. Refresh status before trying again. ${error.message}` : error.message };
      if (error.outcomeUnknown) setStatusAvailable(false, error.message);
    }
  } finally {
    if (state.initializationAction === action) {
      state.initializationAction = null;
      state.initializationRevision += 1;
      renderInitialization(state.initialization);
    }
  }
}

async function runDscgr() {
  if (!state.statusAvailable || state.dscgrInFlight || state.initializationAction
      || !state.initialization?.session_id || state.initialization.status !== "ready_for_3d") {
    return;
  }
  state.dscgrInFlight = true;
  const context = state.contextRevision;
  const sessionId = state.initialization.session_id;
  runDscgrButton.disabled = true;
  renderInitialization(state.initialization);
  dscgrStatus.textContent = "running";
  try {
    const result = await fetchJson("/api/dscgr/run", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (context !== state.contextRevision || sessionId !== state.initialization?.session_id) return;
    const count = Array.isArray(result.processed_ptrs) ? result.processed_ptrs.length : 0;
    dscgrStatus.textContent = `done: ${count} frames, ${result.output_dir || ""}`;
    try {
      await loadStatus();
    } catch (error) {
      dscgrStatus.textContent = `Run completed: ${count} frames. Status refresh failed; wait for reconnection.`;
    }
  } catch (error) {
    if (context === state.contextRevision && sessionId === state.initialization?.session_id) dscgrStatus.textContent = error.message;
  } finally {
    state.dscgrInFlight = false;
    renderInitialization(state.initialization);
  }
}

runParametersButton.addEventListener("click", () => {
  parameterDialogOpener = document.activeElement;
  renderRunParameters();
  runParametersDialog.showModal();
  document.body.classList.add("modal-open");
  document.querySelector("#run-parameters-heading").focus();
});

closeRunParametersButton.addEventListener("click", () => runParametersDialog.close());
runParametersDialog.addEventListener("close", () => {
  document.body.classList.remove("modal-open");
  if (parameterDialogOpener?.isConnected) parameterDialogOpener.focus();
});
runParametersDialog.addEventListener("click", (event) => {
  if (event.target !== runParametersDialog) return;
  const bounds = runParametersDialog.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom) runParametersDialog.close();
});

alignmentMethodSelect.addEventListener("change", () => {
  if (!alignmentSelectable()) {
    renderAlignmentConfiguration();
    return;
  }
  const method = alignmentMethodSelect.value;
  if (state.alignmentCapabilities[method]?.available !== true) return;
  state.alignmentDraft = method;
  state.alignmentDraftEdited = true;
  renderInitialization(state.initialization);
});

refreshImagesButton.addEventListener("click", async () => {
  if (state.sourceActionInFlight) return;
  state.sourceActionInFlight = true;
  const context = state.contextRevision;
  state.sourceError = null;
  renderExperimentSource(state.experimentSource);
  try {
    await Promise.all([loadParameterMetadata(), refreshOverview()]);
    if (!state.imageReady && state.initialization?.session_id) {
      state.currentImageKey = null;
      maybeLoadInitializationImage(state.initialization);
    }
  } catch (error) {
    if (context === state.contextRevision) state.sourceError = error.message;
  } finally {
    state.sourceActionInFlight = false;
    renderExperimentSource(state.experimentSource);
  }
});

fullModeControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-full-mode]");
  if (!button || button.disabled || state.initialization?.current_step?.key !== "is_full") {
    return;
  }
  await performInitializationAction("is-full", { is_full: button.dataset.fullMode === "true" }, "Saving crystal selection…", "Crystal selection saved.");
});

cornerControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-corner]");
  if (!button || button.disabled || !canCompareCorner(state.initialization)) {
    return;
  }
  await performInitializationAction("corner", { corner: button.dataset.corner }, "Updating corner…", "Corner selected.");
});

candidateControls.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-choice]");
  if (!button || button.disabled || !initializationEditable() || !state.initialization?.session_id) {
    return;
  }
  const nextChoice = Number(button.dataset.choice);
  if (state.initialization.selected_3d_choice != null
      && state.initialization.selected_3d_choice !== nextChoice) {
    captureCurrent3DSnapshot();
  }
  await performInitializationAction("3d-choice", { choice: nextChoice }, "Loading candidate preview…", `Previewing 3D choice ${nextChoice}.`);
});

confirm3DChoiceButton.addEventListener("click", async () => {
  if (!initializationEditable() || !state.initialization?.session_id
      || !alignmentSelectionReady()
      || state.initialization?.selected_3d_choice == null
      || state.initialization?.status !== "ready_for_3d") {
    return;
  }
  captureCurrent3DSnapshot();
  const fields = state.alignmentConfiguration ? { alignment_method: state.alignmentDraft } : {};
  const successMessage = state.alignmentConfiguration
    ? "Candidate and alignment confirmed; baseline established." : "Candidate confirmed; baseline established.";
  await performInitializationAction("confirm", fields, "Confirming selected candidate…", successMessage, { refresh: true });
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
  if (!state.initialization?.can_undo) {
    return;
  }
  await performInitializationAction("undo", {}, "Undoing last marking…", "Last marking undone.");
});

resetInitButton.addEventListener("click", async () => {
  await performInitializationAction("reset", {}, "Resetting initialization…", "Initialization reset.", { refresh: true });
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

async function initializePage() {
  await loadUiConfig();
  await Promise.allSettled([loadParameterMetadata(), refreshOverview()]);
}
initializePage();

function refreshPage() {
  Promise.allSettled([loadParameterMetadata(), refreshOverview()]);
}
window.addEventListener("focus", refreshPage);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshPage();
});

window.setInterval(refreshLiveStatus, 1000);
