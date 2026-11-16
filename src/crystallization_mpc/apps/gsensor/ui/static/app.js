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
let parameterDialogOpener = null;

const currentRunId = document.querySelector("#current-run-id");
const currentImageDirectory = document.querySelector("#current-image-directory");
const currentImageCount = document.querySelector("#current-image-count");
const imageSourceStatus = document.querySelector("#image-source-status");
const refreshImagesButton = document.querySelector("#refresh-images");
const imageScanStatus = document.querySelector("#image-scan-status");
const scanProcessedCount = document.querySelector("#scan-processed-count");
const scanPendingCount = document.querySelector("#scan-pending-count");
const scanLatestDiscovered = document.querySelector("#scan-latest-discovered");
const scanLatestReady = document.querySelector("#scan-latest-ready");
const scanSequenceWatermark = document.querySelector("#scan-sequence-watermark");
const scanMissingCount = document.querySelector("#scan-missing-count");
const scanLateCount = document.querySelector("#scan-late-count");
const scanSequenceWarning = document.querySelector("#scan-sequence-warning");
const scanSequenceWarningText = document.querySelector("#scan-sequence-warning-text");
const confirmSequenceResetButton = document.querySelector("#confirm-sequence-reset");
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
const restartInitializationButton = document.querySelector("#restart-initialization");
const reinitializationStatus = document.querySelector("#reinitialization-status");
const markingCurrentImage = document.querySelector("#marking-current-image");
const markingLatestReady = document.querySelector("#marking-latest-ready");
const reinitializationFeedback = document.querySelector("#reinitialization-feedback");
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
  alignmentCapabilitiesReady: false,
  alignmentCapabilitiesError: null,
  initialized: false,
  initialization: null,
  gsensorActivation: null,
  reinitialization: {},
  reinitializationAction: null,
  reinitializationFeedback: null,
  experimentSource: null,
  measurementActive: false,
  experimentLifecycleStatus: "not_started",
  sourceActionInFlight: false,
  statusRead: null,
  sourceRead: null,
  metadataRead: null,
  initializationPayloadRevision: null,
  initializationVisual: null,
  candidateRenderKey: null,
  statusAvailable: false,
  imageScan: {},
  sequenceResetAction: null,
  sequenceResetFeedback: null,
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
  latestMeasurementResult: null,
  latestOverlayFrame: null,
  lastOverlayRefreshAt: 0,
  overlayRequest: 0,
  overlayImage: null,
  alignmentCapabilities: {},
};

function setIfChanged(element, property, value) {
  const next = ["textContent", "className", "title"].includes(property) ? String(value ?? "") : value;
  if (element[property] !== next) element[property] = next;
}

function toggleClass(element, name, enabled) {
  const next = Boolean(enabled);
  if (element.classList.contains(name) !== next) element.classList.toggle(name, next);
  return next;
}

function setAttributeIfChanged(element, name, value) {
  const next = String(value);
  if (element.getAttribute(name) !== next) element.setAttribute(name, next);
}

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
  setIfChanged(measurementOverlay, "hidden", true);
  measurementOverlay.removeAttribute("src");
  setIfChanged(measurementOverlayCaption, "textContent", "No overlay for the current experiment.");
  setIfChanged(refreshOverlayButton, "disabled", true);
}

function setStatusAvailable(available, message = "") {
  state.statusAvailable = available;
  setIfChanged(connectionStatus, "hidden", available);
  setIfChanged(connectionStatus, "textContent", available ? "" : `Status connection lost. Measurements, images and parameters may be outdated. Editing is disabled until status refresh succeeds. ${message}`);
  renderRunParameters();
  renderInitialization(state.initialization);
  renderImageScan(state.imageScan);
  renderReinitialization();
  setIfChanged(refreshOverlayButton, "disabled", !available || !state.latestOverlayFrame);
}

function adoptExperiment(payload) {
  const runId = payload.current_run_id || payload.experiment?.run_id || null;
  if (state.runId === runId) {
    return;
  }
  state.runId = runId;
  state.contextRevision += 1;
  state.initializationRevision += 1;
  state.initializationPayloadRevision = null;
  state.initializationAction = null;
  state.initializationFeedback = null;
  state.gsensorActivation = null;
  state.reinitialization = {};
  state.reinitializationAction = null;
  state.reinitializationFeedback = null;
  state.confirmedSessionId = null;
  state.imageScan = {};
  state.sequenceResetAction = null;
  state.sequenceResetFeedback = null;
  state.experimentParameters = null;
  state.alignmentConfiguration = null;
  state.initialized = false;
  state.latestMeasurementResult = null;
  state.sourceError = null;
  clearMeasurementOverlay();
  renderInitialization(null);
  renderExperimentSource({
    ...(payload.experiment || {}), selected: Boolean(runId), run_id: runId, image_count: 0,
  });
}

function renderStatus(payload, { includeInitialization = true } = {}) {
  adoptExperiment(payload);
  const initializationUnchanged = payload.initialization_unchanged === true
    && payload.initialization_revision === state.initializationPayloadRevision;
  const initialization = initializationUnchanged ? state.initialization : payload.initialization || null;
  state.statusAvailable = true;
  setIfChanged(connectionStatus, "hidden", true);
  state.measurementActive = Boolean(payload.active);
  state.experimentLifecycleStatus = payload.experiment_lifecycle_status || "not_started";
  state.initialized = Boolean(payload.initialized);
  state.gsensorActivation = payload.gsensor_activation || null;
  state.reinitialization = payload.reinitialization || {};
  state.alignmentConfiguration = payload.alignment_configuration || null;
  const snapshot = payload.experiment_parameters;
  // Only the run snapshot is authoritative. /api/params can be a later Central draft.
  state.experimentParameters = snapshot?.run_id === state.runId ? snapshot : null;
  renderRunParameters();
  setIfChanged(statusText, "textContent", state.experimentLifecycleStatus);
  const lifecycleRunning = [
    "waiting_for_initial_image",
    "initializing",
    "baseline_ready",
    "measuring",
  ].includes(state.experimentLifecycleStatus);
  setIfChanged(statusText, "className", state.experimentLifecycleStatus === "error"
    ? "status error"
    : (lifecycleRunning ? "status running" : "status idle"));
  setIfChanged(initializedText, "textContent", payload.initialized ? "initialized" : payload.initialization_status || "not initialized");
  setIfChanged(initializedText, "className", payload.initialized ? "status running" : "status idle");
  if (state.uiMode === "development") {
    setIfChanged(paramsBlock, "textContent", JSON.stringify(payload.params || {}, null, 2));
    setIfChanged(messageBlock, "textContent", JSON.stringify({
    last_message: payload.last_message || null,
    last_command_message: payload.last_command_message || null,
    initialization,
    measurement_running: payload.measurement_running,
    last_measurement_step_at: payload.last_measurement_step_at || null,
    measurement_step_count: payload.measurement_step_count || 0,
    last_dscgr_result: payload.last_dscgr_result || null,
    experiment: payload.experiment || null,
    experiment_selection_status: payload.experiment_selection_status || null,
    experiment_selection_error: payload.experiment_selection_error || null,
    image_scan: payload.image_scan || null,
    gsensor_activation: payload.gsensor_activation || null,
    reinitialization: payload.reinitialization || null,
    }, null, 2));
  }
  renderImageScan(payload.image_scan || {});
  renderBaseline(payload.baseline || null);
  renderOnlineMeasurement(payload);
  if (state.runId && Number.isInteger(payload.image_scan?.file_count)) {
    state.experimentSource = {
      ...(payload.experiment || state.experimentSource || {}), selected: true, run_id: state.runId,
      image_count: payload.image_scan.file_count,
      latest_image: payload.image_scan.latest_ready?.image_name || null,
      latest_discovered_image: payload.image_scan.latest_discovered?.image_name || null,
    };
  }
  renderExperimentSource(state.experimentSource);
  if (includeInitialization || initialization?.session_id !== state.initialization?.session_id) {
    if (initialization?.session_id !== state.initialization?.session_id && state.initializationAction) {
      state.initializationAction = null;
      state.initializationRevision += 1;
    }
    renderInitialization(initialization);
    state.initializationPayloadRevision = payload.initialization_revision || null;
  } else {
    renderInitialization(state.initialization);
  }
  renderReinitialization();
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
    setIfChanged(measurementOverlay, "hidden", false);
    setIfChanged(measurementOverlayCaption, "textContent", `${final ? "Final" : "Latest"} overlay · ${runId} · frame ${frameSeq}`);
  };
  image.onerror = () => {
    if (revision !== state.overlayRequest || runId !== state.runId) return;
    setIfChanged(measurementOverlay, "hidden", true);
    measurementOverlay.removeAttribute("src");
    setIfChanged(measurementOverlayCaption, "textContent", "Overlay is not available yet. Refresh Preview to try again.");
    state.latestOverlayFrame = null;
  };
  image.src = `/api/measurement/overlay/${kind}?run_id=${encodeURIComponent(runId)}&frame_seq=${encodeURIComponent(frameSeq)}&_=${now}`;
  state.latestOverlayFrame = key;
  state.lastOverlayRefreshAt = now;
  setIfChanged(refreshOverlayButton, "disabled", false);
}

function renderOnlineMeasurement(payload) {
  const processing = payload.growth_rate_processing || {};
  const candidateResult = processing.latest_result || null;
  const result = candidateResult && (!candidateResult.run_id || candidateResult.run_id === state.runId) ? candidateResult : null;
  state.latestMeasurementResult = result;
  const publishing = payload.sample_publishing || {};
  const influx = payload.influx_persistence || {};
  const valid = Boolean(result?.valid);
  const hasResult = Boolean(result);

  setIfChanged(measurementValidity, "textContent", hasResult ? (valid ? "valid" : "invalid") : "no sample");
  setIfChanged(measurementValidity, "className", hasResult
    ? (valid ? "status running" : "status error")
    : "status idle");
  setIfChanged(measurementFrame, "textContent", hasResult ? String(result.frame_seq) : "—");
  setIfChanged(measurementImage, "textContent", result?.image_name || "—");
  setIfChanged(measurementGU, "textContent", formatGrowthRate(result?.u?.G, result?.unit));
  setIfChanged(measurementGUKf, "textContent", formatGrowthRate(result?.u?.G_KF, result?.unit));
  setIfChanged(measurementGV, "textContent", formatGrowthRate(result?.v?.G, result?.unit));
  setIfChanged(measurementGVKf, "textContent", formatGrowthRate(result?.v?.G_KF, result?.unit));
  setIfChanged(measurementValidCount, "textContent", String(processing.valid_frame_count || 0));
  setIfChanged(measurementInvalidCount, "textContent", String(processing.invalid_frame_count || 0));
  const alignment = result?.alignment || null;
  const previewMethod = alignment?.method ? alignmentLabel(alignment.method) : "—";
  setIfChanged(alignmentMethod, "textContent", previewMethod);
  setIfChanged(alignmentStatus, "textContent", alignment
    ? (alignment.success ? (alignment.fallback_used ? "fallback" : "success") : "failed")
    : "—");
  setIfChanged(alignmentTranslation, "textContent", alignment
    ? `${Number(alignment.tx_px || 0).toFixed(2)}, ${Number(alignment.ty_px || 0).toFixed(2)} px`
    : "—");
  setIfChanged(alignmentRotation, "textContent", alignment
    ? `${Number(alignment.rotation_deg || 0).toFixed(3)}°`
    : "—");
  setIfChanged(alignmentRuntime, "textContent", alignment
    ? `${Number(alignment.runtime_ms || 0).toFixed(1)} ms`
    : "—");
  setIfChanged(measurementPublishCount, "textContent", String(publishing.success_count || 0));
  setIfChanged(measurementInfluxCount, "textContent", String(influx.success_count || 0));

  const errors = [
    result?.error,
    alignment?.error,
    publishing.last_error,
    influx.last_error,
  ].filter(Boolean);
  setIfChanged(measurementError, "textContent", errors.length
    ? errors.join(" · ")
    : (hasResult ? `Processed at ${result.processed_at}` : "Waiting for the first post-baseline image."));
  toggleClass(measurementError, "error", errors.length > 0);

  const final = ["stopped", "completed"].includes(payload.experiment_lifecycle_status)
    && Boolean(processing.final_overlay_path);
  if (hasResult) {
    refreshMeasurementOverlay(result.frame_seq, { final, processedAt: result.processed_at });
  } else {
    clearMeasurementOverlay();
  }
}

function renderBaseline(baseline) {
  const ready = Boolean(baseline && (
    baseline.segment_frame_seq === 0
    || (baseline.segment_frame_seq == null && baseline.frame_seq === 0)
  ));
  setIfChanged(baselineStatus, "textContent", ready ? "ready" : "not ready");
  setIfChanged(baselineStatus, "className", ready ? "status running" : "status idle");
  setIfChanged(baselineFrameSeq, "textContent", ready ? String(baseline.frame_seq) : "—");
  setIfChanged(baselineImage, "textContent", ready ? baseline.image_name : "—");
  setIfChanged(baselineUDistance, "textContent", ready ? `${baseline.u?.distance_px ?? 0} px` : "—");
  setIfChanged(baselineVDistance, "textContent", ready ? `${baseline.v?.distance_px ?? 0} px` : "—");
  setIfChanged(baselineEstablishedAt, "textContent", ready ? baseline.established_at : "—");
}

function renderImageScan(scan) {
  state.imageScan = scan && typeof scan === "object" ? scan : {};
  const currentScan = state.imageScan;
  const scanStatus = currentScan.status || "stopped";
  setIfChanged(imageScanStatus, "textContent", scanStatus);
  setIfChanged(imageScanStatus, "className", scanStatus === "error" ? "status error" : (
    ["running", "waiting_for_image", "monitoring_during_initialization"].includes(scanStatus)
      ? "status running" : "status idle"
  ));
  setIfChanged(scanProcessedCount, "textContent", String(currentScan.processed_count || 0));
  setIfChanged(scanPendingCount, "textContent", String(currentScan.pending_image_count || 0));
  setIfChanged(scanLatestDiscovered, "textContent", formatSourceImage(currentScan.latest_discovered));
  setIfChanged(scanLatestReady, "textContent", formatSourceImage(currentScan.latest_ready));

  const sequenceHealth = currentScan.sequence_health || {};
  const watermark = currentScan.watermark || null;
  setIfChanged(scanSequenceWatermark, "textContent", formatWatermark(watermark, sequenceHealth.watermark_sequence));
  const missingCount = Number(sequenceHealth.missing_count || 0);
  const lateCount = Number(sequenceHealth.late_count || 0);
  setIfChanged(scanMissingCount, "textContent", String(Number.isFinite(missingCount) ? missingCount : 0));
  setIfChanged(scanLateCount, "textContent", String(Number.isFinite(lateCount) ? lateCount : 0));
  toggleClass(scanMissingCount, "sequence-alert", missingCount > 0);
  toggleClass(scanLateCount, "sequence-alert", lateCount > 0);
  setIfChanged(scanMissingCount, "title", formatMissingRanges(sequenceHealth.missing_ranges));
  setIfChanged(scanLateCount, "title", sequenceHealth.last_late
    ? `Latest late image: ${formatSourceImage(sequenceHealth.last_late)}` : "");

  const resetCandidate = sequenceHealth.reset_candidate || null;
  const resetAvailable = sequenceHealth.reset_available === true
    && Boolean(resetCandidate?.identity_key);
  const resetAction = state.sequenceResetAction;
  const visibleCandidate = resetAvailable ? resetCandidate : resetAction?.candidate;
  if (state.sequenceResetFeedback?.kind === "success" && resetAvailable) {
    state.sequenceResetFeedback = null;
  }
  const feedback = state.sequenceResetFeedback;
  const showReset = Boolean(resetAvailable || resetAction);
  setIfChanged(scanSequenceWarning, "hidden", !showReset && !feedback);
  toggleClass(scanSequenceWarning, "pending", Boolean(resetAction));
  toggleClass(scanSequenceWarning, "error", feedback?.kind === "error");
  setIfChanged(confirmSequenceResetButton, "hidden", !showReset);
  setIfChanged(confirmSequenceResetButton, "disabled", !state.statusAvailable
    || Boolean(resetAction)
    || !state.runId
    || !visibleCandidate?.identity_key);
  setIfChanged(confirmSequenceResetButton, "textContent", resetAction
    ? "Confirming numbering restart…" : "Confirm numbering restart");

  if (resetAction) {
    setIfChanged(scanSequenceWarningText, "textContent", `Confirming a new source-number sequence from ${formatSourceImage(visibleCandidate)}. The current marking image remains unchanged.`);
  } else if (feedback) {
    setIfChanged(scanSequenceWarningText, "textContent", feedback.message);
  } else if (showReset) {
    setIfChanged(scanSequenceWarningText, "textContent", `Possible camera numbering restart at ${formatSourceImage(visibleCandidate)}. Confirm only if the camera/source numbering actually restarted. A normally late image must remain classified as late.`);
  } else {
    setIfChanged(scanSequenceWarningText, "textContent", "");
  }

  setIfChanged(scanLastImage, "textContent", currentScan.last_detected_image || "—");
  setIfChanged(scanFileModifiedAt, "textContent", currentScan.file_modified_at || "—");
  setIfChanged(scanDetectedAt, "textContent", currentScan.detected_at || "—");
  setIfChanged(scanLastError, "textContent", currentScan.error || "");
  setIfChanged(scanLastError, "hidden", !currentScan.error);
  renderReinitialization();
}

function formatSourceImage(image) {
  if (!image || typeof image !== "object") return "—";
  const name = image.image_name || "unnamed image";
  return image.source_sequence == null ? name : `${name} · seq ${image.source_sequence}`;
}

function formatWatermark(watermark, fallbackSequence) {
  if (watermark && typeof watermark === "object") {
    if (watermark.image_name) return formatSourceImage(watermark);
    if (watermark.source_sequence != null) return `seq ${watermark.source_sequence}`;
  }
  return fallbackSequence == null ? "—" : `seq ${fallbackSequence}`;
}

function formatMissingRanges(ranges) {
  if (!Array.isArray(ranges) || ranges.length === 0) return "";
  return ranges.map((range) => {
    const start = range?.start;
    const end = range?.end;
    if (start == null || end == null) return null;
    return start === end ? String(start) : `${start}–${end}`;
  }).filter(Boolean).join(", ");
}

function currentInitializationSessionId() {
  return state.initialization?.session_id ?? null;
}

function initializationImageName(value) {
  if (typeof value !== "string" || !value) return "—";
  return value.split(/[\\/]/).filter(Boolean).at(-1) || value;
}

function reinitializationActionAllowed() {
  return state.statusAvailable
    && Boolean(state.runId)
    && state.reinitialization?.can_restart === true
    && state.reinitialization?.in_progress !== true
    && !state.reinitializationAction
    && !state.initializationAction

    && !state.dscgrInFlight;
}

function renderReinitialization() {
  const lifecycle = state.experimentLifecycleStatus;
  const action = state.reinitializationAction;
  const serverPending = state.reinitialization?.in_progress === true;
  let localFeedback = state.reinitializationFeedback;
  if (localFeedback?.kind === "success" && (
    localFeedback.sessionId !== currentInitializationSessionId()
    || lifecycle !== "initializing"
    || state.initialized
    || state.alignmentConfiguration?.confirmed
  )) {
    state.reinitializationFeedback = null;
    localFeedback = null;
  }
  const serverError = state.reinitialization?.last_error;
  const feedback = localFeedback || (serverError ? {
    kind: "error",
    message: `The latest image could not be selected. ${serverError}`,
  } : null);

  setIfChanged(markingCurrentImage, "textContent", initializationImageName(state.initialization?.selected_image));
  setIfChanged(markingLatestReady, "textContent", formatSourceImage(state.imageScan?.latest_ready));

  const measuring = lifecycle === "measuring" || lifecycle === "baseline_ready";
  setIfChanged(restartInitializationButton, "textContent", action || serverPending
    ? "Switching to latest image…"
    : (measuring ? "Re-mark on latest image" : "Use latest image"));
  setIfChanged(restartInitializationButton, "disabled", !reinitializationActionAllowed());
  setAttributeIfChanged(restartInitializationButton, "aria-busy", Boolean(action || serverPending));

  if (!state.statusAvailable) {
    setIfChanged(reinitializationStatus, "textContent", "connection lost");
    setIfChanged(reinitializationStatus, "className", "status error");
  } else if (action || serverPending) {
    setIfChanged(reinitializationStatus, "textContent", "switching");
    setIfChanged(reinitializationStatus, "className", "status running");
  } else if (state.reinitialization?.can_restart === true) {
    setIfChanged(reinitializationStatus, "textContent", "ready");
    setIfChanged(reinitializationStatus, "className", "status running");
  } else {
    setIfChanged(reinitializationStatus, "textContent", "unavailable");
    setIfChanged(reinitializationStatus, "className", "status idle");
  }

  setIfChanged(reinitializationFeedback, "hidden", !feedback);
  setIfChanged(reinitializationFeedback, "textContent", feedback?.message || "");
  toggleClass(reinitializationFeedback, "error", feedback?.kind === "error");
  toggleClass(reinitializationFeedback, "pending", feedback?.kind === "pending");
}

async function restartInitializationOnLatestImage() {
  if (!reinitializationActionAllowed()) return;
  const controlRevision = state.gsensorActivation?.control_revision;
  if (!Number.isInteger(controlRevision) || controlRevision < 0) {
    state.reinitializationFeedback = {
      kind: "error",
      message: "GSensor activation status is incomplete. Refresh status before switching the marking image.",
    };
    renderReinitialization();
    return;
  }

  const latestReady = formatSourceImage(state.imageScan?.latest_ready);

  const statusRequest = ++state.statusRequest;
  const initializationRevision = ++state.initializationRevision;
  const action = {
    context: state.contextRevision,
    runId: state.runId,
    sessionId: currentInitializationSessionId(),
    controlRevision,
    statusRequest,
    initializationRevision,
    latestReady,
  };
  state.reinitializationAction = action;
  state.reinitializationFeedback = {
    kind: "pending",
    message: `Switching the marking session to ${latestReady}. Waiting for the frozen image and cleared point set…`,
  };
  renderInitialization(state.initialization);
  renderExperimentSource(state.experimentSource);

  const current = () => state.reinitializationAction === action
    && state.contextRevision === action.context
    && state.runId === action.runId
    && currentInitializationSessionId() === action.sessionId
    && state.gsensorActivation?.control_revision === action.controlRevision
    && state.statusRequest === action.statusRequest
    && state.initializationRevision === action.initializationRevision;

  try {
    const payload = await fetchJson("/api/initialization/restart", {
      method: "POST",
      body: JSON.stringify({
        run_id: action.runId,
        session_id: action.sessionId,
        control_revision: action.controlRevision,
      }),
    });
    if (!current()) return;

    const responseRunId = payload.current_run_id || payload.experiment?.run_id || null;
    const responseControlRevision = payload.gsensor_activation?.control_revision;
    if (responseRunId !== action.runId
        || responseControlRevision !== action.controlRevision
        || !payload.reinitialization
        || !("initialization" in payload)) {
      throw new Error("The switch response does not match the current experiment. Refresh status before trying again.");
    }

    state.reinitializationAction = null;
    state.initializationRevision += 1;
    renderStatus(payload, { includeInitialization: true });
    state.reinitializationFeedback = {
      kind: "success",
      sessionId: currentInitializationSessionId(),
      message: `Now marking ${initializationImageName(state.initialization?.selected_image)}. Previous points were cleared. Complete and confirm this marking before new G values resume.`,
    };
    renderReinitialization();
  } catch (error) {
    if (current()) {
      state.reinitializationFeedback = {
        kind: "error",
        message: error.outcomeUnknown
          ? `The switch outcome is unknown. Wait for status to reconnect before trying again. ${error.message}`
          : `The marking image was not switched. ${error.message}`,
      };
      if (error.outcomeUnknown) setStatusAvailable(false, error.message);
    }
  } finally {
    if (state.reinitializationAction === action) {
      state.reinitializationAction = null;
      if (state.reinitializationFeedback?.kind === "pending") {
        state.reinitializationFeedback = null;
      }
      state.initializationRevision += 1;
      renderInitialization(state.initialization);
      renderExperimentSource(state.experimentSource);
    }
  }
}

async function confirmSequenceReset() {
  const sequenceHealth = state.imageScan?.sequence_health || {};
  const candidate = sequenceHealth.reset_candidate || null;
  if (!state.statusAvailable || state.sequenceResetAction
      || !state.runId
      || sequenceHealth.reset_available !== true || !candidate?.identity_key) {
    return;
  }

  const accepted = window.confirm(
    `Confirm camera/source numbering restart?\n\nCandidate: ${formatSourceImage(candidate)}\n\nThis accepts the current candidate as the start of restarted numbering and continues with its source number. Confirm only when numbering truly restarted; do not use this for a normally late image. The image currently open for marking will not change.`,
  );
  if (!accepted) return;

  const action = {
    context: state.contextRevision,
    runId: state.runId,
    candidateIdentityKey: candidate.identity_key,
    candidate: { ...candidate },
  };
  state.sequenceResetAction = action;
  state.sequenceResetFeedback = null;
  renderImageScan(state.imageScan);
  const current = () => state.sequenceResetAction === action
    && state.contextRevision === action.context
    && state.runId === action.runId;

  try {
    const payload = await fetchJson("/api/image-scan/confirm-sequence-reset", {
      method: "POST",
      body: JSON.stringify({
        run_id: action.runId,
        candidate_identity_key: action.candidateIdentityKey,
      }),
    });
    if (!current()) return;

    if (payload?.image_scan) {
      const responseRunId = payload.current_run_id || payload.experiment?.run_id || action.runId;
      if (responseRunId === action.runId) {
        renderStatus(payload, { includeInitialization: !state.initializationAction });
      }
    } else {
      try {
        await loadStatus();
      } catch (error) {
        if (current()) {
          state.sequenceResetFeedback = {
            kind: "error",
            message: "Numbering restart was accepted, but status refresh failed. Wait for the status connection to recover before taking another action.",
          };
          setStatusAvailable(false, error.message);
        }
        return;
      }
    }
    if (current()) {
      state.sequenceResetFeedback = {
        kind: "success",
        message: `Numbering restart confirmed from ${formatSourceImage(action.candidate)}. New images will continue in the new source-number sequence; the current marking image was not changed.`,
      };
    }
  } catch (error) {
    if (current()) {
      state.sequenceResetFeedback = {
        kind: "error",
        message: error.outcomeUnknown
          ? `Request outcome is unknown. Refresh status before trying again. ${error.message}`
          : `Numbering restart was not confirmed. ${error.message}`,
      };
      if (error.outcomeUnknown) setStatusAvailable(false, error.message);
    }
  } finally {
    if (state.sequenceResetAction === action) {
      state.sequenceResetAction = null;
      renderImageScan(state.imageScan);
    }
  }
}

function renderExperimentSource(payload) {
  state.experimentSource = payload;
  const selected = Boolean(payload?.selected);
  const imageCount = Number(payload?.image_count || 0);
  setIfChanged(currentRunId, "textContent", selected ? payload.run_id : "No experiment selected");
  setIfChanged(currentImageDirectory, "textContent", selected ? (payload.container_image_path || "—") : "—");
  setIfChanged(currentImageCount, "textContent", String(imageCount));

  setIfChanged(refreshImagesButton, "disabled", state.sourceActionInFlight
    || Boolean(state.reinitializationAction));

  toggleClass(imageSourceStatus, "error", Boolean(state.sourceError));
  if (state.sourceError) {
    setIfChanged(imageSourceStatus, "textContent", `Image list refresh failed: ${state.sourceError}`);
  } else if (!selected) {
    setIfChanged(imageSourceStatus, "textContent", "Create or select an experiment in Central first.");
  } else if (state.experimentLifecycleStatus === "waiting_for_initial_image") {
    setIfChanged(imageSourceStatus, "textContent", "Waiting for the latest ready camera image. Initialization will open automatically.");
  } else if (state.experimentLifecycleStatus === "initializing") {
    setIfChanged(imageSourceStatus, "textContent", "The latest ready image is fixed as the current marking baseline. Directory monitoring continues without replacing the image currently open below.");
  } else if (["baseline_ready", "measuring"].includes(state.experimentLifecycleStatus)) {
    setIfChanged(imageSourceStatus, "textContent", "The current marking baseline is ready. The directory is being scanned for later images.");
  } else if (imageCount === 0) {
    setIfChanged(imageSourceStatus, "textContent", "Start the experiment in Central; Gsensor will wait for the latest ready camera image.");
  } else {
    setIfChanged(imageSourceStatus, "textContent", `${imageCount} images available. Start the experiment in Central to use the latest ready image as the baseline candidate.`);
  }
}

function loadStatus() {
  if (state.statusRead) return state.statusRead;
  const reading = requestStatus();
  state.statusRead = reading;
  reading.finally(() => { if (state.statusRead === reading) state.statusRead = null; }).catch(() => {});
  return reading;
}

async function requestStatus() {
  const request = ++state.statusRequest;
  const initializationRevision = state.initializationRevision;
  try {
    const query = state.initializationPayloadRevision
      ? `?initialization_revision=${encodeURIComponent(state.initializationPayloadRevision)}` : "";
    const payload = await fetchJson(`/api/status${query}`);
    if (request !== state.statusRequest || initializationRevision !== state.initializationRevision) return null;
    renderStatus(payload, { includeInitialization: !state.initializationAction });
    return payload;
  } catch (error) {
    if (request !== state.statusRequest || initializationRevision !== state.initializationRevision) return null;
    setStatusAvailable(false, error.message);
    throw error;
  }
}

function loadExperimentSource() {
  if (state.sourceRead) return state.sourceRead;
  const reading = requestExperimentSource();
  state.sourceRead = reading;
  reading.finally(() => { if (state.sourceRead === reading) state.sourceRead = null; }).catch(() => {});
  return reading;
}

async function requestExperimentSource() {
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
  if (state.statusRead || state.reinitializationAction
      || document.hidden) {
    return;
  }
  try {
    const payload = await loadStatus();
    if (!payload) return;
    const statusRunId = payload.current_run_id || payload.experiment?.run_id || null;
    const displayedRunId = state.experimentSource?.run_id || null;
    if (statusRunId !== displayedRunId || (statusRunId && state.experimentSource?.image_count === 0
        && !Number.isInteger(payload.image_scan?.file_count))) {
      await loadExperimentSource().catch(() => {});
    }
  } catch (error) {
    setIfChanged(statusText, "textContent", error.message);
    setIfChanged(statusText, "className", "status error");
  }
}

function loadParameterMetadata() {
  if (state.metadataRead) return state.metadataRead;
  const reading = requestParameterMetadata();
  state.metadataRead = reading;
  reading.finally(() => { if (state.metadataRead === reading) state.metadataRead = null; }).catch(() => {});
  return reading;
}

async function requestParameterMetadata() {
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

function alignmentSelectionReady() {
  const method = state.alignmentConfiguration?.method || state.alignmentConfiguration?.configured_method;
  return Boolean(method) && state.alignmentCapabilitiesReady && state.alignmentCapabilities[method]?.available === true;
}

function renderRunParameters() {
  const snapshot = state.experimentParameters;
  setIfChanged(parameterRunLabel, "textContent", snapshot?.label || "—");
  setIfChanged(parameterRunId, "textContent", snapshot?.run_id || state.runId || "—");
  setIfChanged(parameterRunVersion, "textContent", snapshot?.parameter_version ?? "—");
  const configuration = snapshot?.alignment_configuration;
  const alignmentMessage = configuration?.confirmed
    ? `Confirmed alignment: ${alignmentLabel(configuration.method)}.`
    : "Alignment method is selected in Central before Start Experiment and is read-only here.";
  const message = !snapshot
    ? (state.runId ? "No run parameter snapshot is available. Parameters become available after Start in Central."
      : "No experiment has started. Parameters become available after Start in Central.")
    : `Effective run parameters · ${alignmentMessage}`;
  setIfChanged(runParametersStatus, "textContent", state.statusAvailable ? message
    : `Status unavailable; displayed values may be outdated. ${message}`);
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
      setIfChanged(group, "className", "run-parameter-group");
      const heading = document.createElement("h3");
      setIfChanged(heading, "textContent", section);
      const list = document.createElement("dl");
      setIfChanged(list, "className", "run-parameter-list");
      group.append(heading, list);
      groups.set(section, list);
      runParametersValues.appendChild(group);
    }
    const row = document.createElement("div");
    const label = document.createElement("dt");
    setIfChanged(label, "textContent", `${meta.label || key}${meta.unit ? ` (${meta.unit})` : ""}`);
    if (meta.label && meta.label !== key) {
      const parameterKey = document.createElement("code");
      setIfChanged(parameterKey, "textContent", key);
      label.appendChild(parameterKey);
    }
    const content = document.createElement("dd");
    setIfChanged(content, "textContent", typeof value === "string" ? value : JSON.stringify(value, null, 2));
    row.append(label, content);
    groups.get(section).appendChild(row);
  });
}

function initializationEditable() {
  return state.statusAvailable && state.experimentLifecycleStatus === "initializing"
    && !state.initialized && !state.alignmentConfiguration?.confirmed
    && !state.initializationAction && !state.reinitializationAction
    && state.reinitialization?.in_progress !== true
    && !state.dscgrInFlight
    && (!state.confirmedSessionId || state.confirmedSessionId !== state.initialization?.session_id);
}

function renderInitialization(payload) {
  if (state.initialization?.session_id !== payload?.session_id) {
    state.initializationFeedback = null;
    state.confirmedSessionId = null;
  }
  ensure3DSnapshotScope(payload);
  state.initialization = payload;
  const hasSession = Boolean(payload?.session_id);
  const step = payload?.current_step || null;
  const selected3DChoice = payload?.selected_3d_choice;
  const editable = initializationEditable();
  setIfChanged(initStepTitle, "textContent", hasSession ? payload.status : "Not started");
  setIfChanged(initStepPrompt, "textContent", step?.prompt || (selected3DChoice
    ? (state.experimentLifecycleStatus === "initializing"
      ? `Previewing 3D choice ${selected3DChoice}. Compare other candidates or confirm this selection.`
      : `Confirmed 3D choice ${selected3DChoice}.`)
    : (hasSession ? "Waiting for the next initialization step." : "Start the experiment in Central. Marking opens when a latest ready image is available.")));
  const feedback = state.initializationFeedback;
  setIfChanged(initSaveStatus, "textContent", feedback?.message || (hasSession ? `${payload.selected_image || ""}` : "idle"));
  toggleClass(initSaveStatus, "error", feedback?.kind === "error");
  toggleClass(initSaveStatus, "pending", Boolean(state.initializationAction));
  setAttributeIfChanged(initCanvas, "aria-busy", Boolean(state.initializationAction));
  setAttributeIfChanged(initCanvas, "aria-disabled", !editable || !state.imageReady || step?.type !== "point");

  setIfChanged(undoInitButton, "disabled", !editable || !payload?.can_undo);
  setIfChanged(resetInitButton, "disabled", !editable || !hasSession);
  setIfChanged(confirm3DChoiceButton, "disabled", !editable
    || !alignmentSelectionReady()
    || payload?.status !== "ready_for_3d"
    || selected3DChoice == null);
  setIfChanged(runDscgrButton, "disabled", !state.statusAvailable || payload?.status !== "ready_for_3d"
    || state.dscgrInFlight || Boolean(state.initializationAction)
    || Boolean(state.reinitializationAction) || state.reinitialization?.in_progress === true);

  fullModeControls.querySelectorAll("button").forEach((button) => {
    setIfChanged(button, "disabled", !editable || !hasSession || step?.key !== "is_full");
    const selected = String(payload?.is_full) === button.dataset.fullMode;
    toggleClass(button, "selected", selected);
  });

  renderCandidateControls(payload);
  renderCornerControls(payload);
  renderCornerReferenceInset(payload);
  maybeLoadInitializationImage(payload);
  const previous = state.initializationVisual;
  if (previous?.payload !== payload || previous?.imageKey !== state.currentImageKey
      || previous?.imageReady !== state.imageReady) {
    const key = JSON.stringify([state.runId, payload, state.currentImageKey, state.imageReady]);
    if (previous?.key !== key) {
      renderPointList(payload);
      drawInitialization();
      drawSelected3DPreview();
    }
    state.initializationVisual = { payload, imageKey: state.currentImageKey, imageReady: state.imageReady, key };
  }
  renderReinitialization();
}

function renderCandidateControls(payload) {
  const candidates = payload?.candidates_3d || [];
  const key = JSON.stringify([payload?.session_id, candidates.map(item => [item.choice, item.label])]);
  if (key !== state.candidateRenderKey) {
    candidateControls.replaceChildren();
    candidates.forEach(candidate => {
      const button = document.createElement("button");
      button.type = "button";
      setIfChanged(button, "className", "ghost");
      button.dataset.choice = String(candidate.choice);
      setIfChanged(button, "textContent", candidate.label || `Choice ${candidate.choice}`);
      candidateControls.appendChild(button);
    });
    state.candidateRenderKey = key;
  }
  setIfChanged(candidateControlsWrap, "hidden", candidates.length === 0);
  candidateControls.querySelectorAll("button").forEach(button => {
    setIfChanged(button, "disabled", !initializationEditable()
      || !["ready_for_3d_choice", "ready_for_3d"].includes(payload?.status));
    toggleClass(button, "selected", String(payload?.selected_3d_choice) === button.dataset.choice);
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
    setIfChanged(button, "disabled", !enabled);
    toggleClass(button, "selected", activeCorner === button.dataset.corner);
  });
}

function renderCornerReferenceInset(payload) {
  const corner = activeCornerReference(payload);
  if (!corner || !state.imageReady) {
    setIfChanged(cornerReferenceInset, "hidden", true);
    cornerReferenceImage.removeAttribute("src");
    return;
  }

  const source = `/static/imgs/corner_${corner}.jpg`;
  if (cornerReferenceImage.getAttribute("src") !== source) cornerReferenceImage.src = source;
  setIfChanged(cornerReferenceImage, "alt", `Corner ${corner}`);
  setIfChanged(cornerReferenceInset, "hidden", false);
}

function appendPointListItem(key, point, computed) {
  const item = document.createElement("li");
  const coords = Array.isArray(point) ? point.slice(0, 2).map((value) => Number(value).toFixed(1)).join(", ") : "";
  setIfChanged(item, "textContent", `${computed ? "*" : ""}${key}: ${coords}`);
  pointList.appendChild(item);
}

function maybeLoadInitializationImage(payload) {
  if (!payload?.session_id) {
    state.currentImageKey = null;
    initImage = null;
    state.imageReady = false;
    setIfChanged(initCanvas, "width", 0);
    setIfChanged(initCanvas, "height", 0);
    setIfChanged(canvasEmpty, "hidden", false);
    setIfChanged(canvasEmpty, "textContent", "No image loaded");
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
  setIfChanged(canvasEmpty, "hidden", false);
  setIfChanged(canvasEmpty, "textContent", "Loading initialization image…");
  const image = new Image();
  initImage = image;
  image.onload = () => {
    if (state.currentImageKey !== imageKey || initImage !== image) return;
    state.imageReady = true;
    initCanvas.width = image.naturalWidth;
    initCanvas.height = image.naturalHeight;
    setIfChanged(canvasEmpty, "hidden", true);
    renderInitialization(state.initialization);
  };
  image.onerror = () => {
    if (state.currentImageKey !== imageKey || initImage !== image) return;
    state.imageReady = false;
    setIfChanged(canvasEmpty, "hidden", false);
    setIfChanged(canvasEmpty, "textContent", "Image preview could not be loaded. Refresh Images to try again.");
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
  render3DSnapshots();
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
  setIfChanged(threeDSnapshots, "hidden", items.length === 0);
  setIfChanged(clear3DSnapshotsButton, "disabled", items.length === 0);
  threeDSnapshotGrid.innerHTML = "";
  items.forEach((item) => {
    const wrapper = document.createElement("figure");
    setIfChanged(wrapper, "className", "three-d-snapshot");
    const image = document.createElement("img");
    image.src = item.image;
    image.alt = item.label;
    const caption = document.createElement("span");
    setIfChanged(caption, "textContent", `${item.label} (${formatRadians(item.yaw)}, ${formatRadians(item.pitch)})`);
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
  setIfChanged(threeDPreview, "hidden", !patch);
  setIfChanged(reset3DViewButton, "disabled", !patch);
  if (!patch) {
    clear3DPreview();
    return;
  }
  setIfChanged(threeDPreviewTitle, "textContent", choice ? `Choice ${choice}` : "Selected candidate");
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
      || state.reinitializationAction || state.reinitialization?.in_progress === true
      || !state.initialization?.session_id || state.initialization.status !== "ready_for_3d") {
    return;
  }
  state.dscgrInFlight = true;
  const context = state.contextRevision;
  const sessionId = state.initialization.session_id;
  setIfChanged(runDscgrButton, "disabled", true);
  renderInitialization(state.initialization);
  setIfChanged(dscgrStatus, "textContent", "running");
  try {
    const result = await fetchJson("/api/dscgr/run", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (context !== state.contextRevision || sessionId !== state.initialization?.session_id) return;
    const count = Array.isArray(result.processed_ptrs) ? result.processed_ptrs.length : 0;
    setIfChanged(dscgrStatus, "textContent", `done: ${count} frames, ${result.output_dir || ""}`);
    try {
      await loadStatus();
    } catch (error) {
      setIfChanged(dscgrStatus, "textContent", `Run completed: ${count} frames. Status refresh failed; wait for reconnection.`);
    }
  } catch (error) {
    if (context === state.contextRevision && sessionId === state.initialization?.session_id) setIfChanged(dscgrStatus, "textContent", error.message);
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

refreshImagesButton.addEventListener("click", async () => {
  if (state.sourceActionInFlight || state.reinitializationAction) return;
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

confirmSequenceResetButton.addEventListener("click", () => {
  confirmSequenceReset();
});

restartInitializationButton.addEventListener("click", () => {
  restartInitializationOnLatestImage();
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
  const fields = { alignment_method: state.alignmentConfiguration.method };
  const successMessage = "Candidate and alignment confirmed; baseline established.";
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
  setIfChanged(measurementOverlay, "hidden", true);
  setIfChanged(measurementOverlayCaption, "textContent", "Overlay is not available yet.");
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
    setIfChanged(initSaveStatus, "textContent", error.message);
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
  if (state.reinitializationAction) return;
  Promise.allSettled([loadParameterMetadata(), refreshOverview()]);
}
window.addEventListener("focus", refreshPage);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshPage();
});

window.setInterval(refreshLiveStatus, 1000);
