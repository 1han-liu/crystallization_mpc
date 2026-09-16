const state = {
  uiMode: "production",
  params: null,
  paramMeta: {},
  target: "sigma",
  runConfiguration: null,
  runConfigurationDefaults: null,
  runConfigurationUpdatedAt: null,
  runConfigurationInFlight: false,
  runConfigurationError: null,
  runConfigurationRefreshError: null,
  drawerOpen: false,
  experiments: [],
  currentRunId: null,
  lastRenderedRunId: null,
  lastRenderedExperimentStatus: null,
  experimentActionInFlight: false,
  experimentActionName: null,
  parameterActionInFlight: false,
  parameterError: null,
  parameterRefreshError: null,
  parameterUnsavedCount: 0,
  latestOverlayKey: null,
  lastOverlayRefreshAt: 0,
  controllerStatus: null,
  seedActionInFlight: false,
  seedActionRunId: null,
  pendingSeedEventId: null,
  seedActionMessage: "",
  seedActionError: false,
  runtimeInFlight: false,
  runtimeRequest: null,
  runtimeError: "",
  runtimeHistory: [],
  runtimeHistoryError: "",
  historyRunId: null,
  historyCursor: null,
  historyLoading: false,
  systemStatusFresh: false,
  systemStatusInFlight: 0,
  experimentRequestId: 0,
  operationRequestId: 0,
  parameterRequestId: 0,
  configurationRequestId: 0,
  gsensorStatusRunId: null,
  selectionSentRunId: null,
  selectionRetryRunId: null,
};

const shell = document.querySelector(".shell");
const toggleParametersButton = document.querySelector("#toggle-parameters");
const commandStatus = document.querySelector("#command-status");
const commandResult = document.querySelector("#command-result");
const derivedPreview = document.querySelector("#derived-preview");
const resetParamsButton = document.querySelector("#reset-params");
const saveParamsButton = document.querySelector("#save-params");
const parameterStatus = document.querySelector("#parameter-status");
const parameterStatusText = document.querySelector("#parameter-status-text");
const refreshPreviewButton = document.querySelector("#refresh-preview");
const sharedForm = document.querySelector("#shared-form");
const gsensorForm = document.querySelector("#gsensor-form");
const controllerForm = document.querySelector("#controller-form");
const fieldTemplate = document.querySelector("#param-field-template");
const experimentStatus = document.querySelector("#experiment-status");
const experimentRunId = document.querySelector("#experiment-run-id");
const experimentDisplayName = document.querySelector("#experiment-display-name");
const experimentCreatedAt = document.querySelector("#experiment-created-at");
const experimentStartedAt = document.querySelector("#experiment-started-at");
const experimentEndedAt = document.querySelector("#experiment-ended-at");
const cameraSavePath = document.querySelector("#camera-save-path");
const copyCameraPathButton = document.querySelector("#copy-camera-path");
const newExperimentLabel = document.querySelector("#new-experiment-label");
const newExperimentButton = document.querySelector("#new-experiment");
const startExperimentButton = document.querySelector("#start-experiment");
const retrySelectionButton = document.querySelector("#retry-selection");
const endExperimentButton = document.querySelector("#end-experiment");
const experimentMessage = document.querySelector("#experiment-message");
const systemRefreshStatus = document.querySelector("#system-refresh-status");
const gsensorLiveStatus = document.querySelector("#gsensor-live-status");
const gsensorLiveFrame = document.querySelector("#gsensor-live-frame");
const gsensorLiveImage = document.querySelector("#gsensor-live-image");
const gsensorMessageCount = document.querySelector("#gsensor-message-count");
const gsensorReceivedAt = document.querySelector("#gsensor-received-at");
const gsensorLiveError = document.querySelector("#gsensor-live-error");
const controllerLiveStatus = document.querySelector("#controller-live-status");
const controllerRunId = document.querySelector("#controller-run-id");
const controllerLastFrame = document.querySelector("#controller-last-frame");
const controllerValidCount = document.querySelector("#controller-valid-count");
const controllerInvalidCount = document.querySelector("#controller-invalid-count");
const controllerSeedCount = document.querySelector("#controller-seed-count");
const controllerLastSeedAt = document.querySelector("#controller-last-seed-at");
const addSeedButton = document.querySelector("#add-seed");
const seedActionMessage = document.querySelector("#seed-action-message");
const controllerAdaptationStatus = document.querySelector("#controller-adaptation-status");
const controllerAdaptationMode = document.querySelector("#controller-adaptation-mode");
const adaptationActionMessage = document.querySelector("#adaptation-action-message");
const controllerLiveError = document.querySelector("#controller-live-error");
const centralOverlayImage = document.querySelector("#central-overlay-image");
const centralOverlayCaption = document.querySelector("#central-overlay-caption");
const centralRefreshOverlay = document.querySelector("#central-refresh-overlay");
const runConfigurationStatus = document.querySelector("#run-configuration-status");
const runConfigurationMessage = document.querySelector("#run-configuration-message");
const runTypeSelect = document.querySelector("#run-type");
const controllerModeSelect = document.querySelector("#controller-mode");
const controlTargetSelect = document.querySelector("#control-target");
const adaptationEnabledSelect = document.querySelector("#adaptation-enabled");
const adaptationModeSelect = document.querySelector("#adaptation-mode");
const growthRateSourceSelect = document.querySelector("#growth-rate-source");
const adaptationEnableLabel = document.querySelector("#adaptation-enable-label");
const adaptiveCheckboxes = [...document.querySelectorAll("[data-adaptive-parameter]")];
const sigmaSetInput = document.querySelector("#runtime-sigma-set");
const growthSetInput = document.querySelector("#runtime-g-set");
const retryRuntimeButton = document.querySelector("#retry-runtime-update");
const setpointInputs = { sigma_set: sigmaSetInput, G_set: growthSetInput };
const adaptiveCombinations = {
  E_A: ["E_A"], k_0: ["k_0"], n: ["n"],
  E_A_and_k_0: ["E_A", "k_0"], E_A_and_n: ["E_A", "n"],
  k_0_and_n: ["k_0", "n"], all: ["E_A", "k_0", "n"],
};

const runConfigurationControls = [
  runTypeSelect,
  controllerModeSelect,
  controlTargetSelect,
  adaptationEnabledSelect,
  adaptationModeSelect,
  growthRateSourceSelect,
];

function mutationInFlight() {
  return state.experimentActionInFlight || state.parameterActionInFlight
    || state.runConfigurationInFlight || state.seedActionInFlight
    || state.runtimeInFlight;
}

function invalidatePendingReads() {
  state.experimentRequestId += 1;
  state.operationRequestId += 1;
  state.parameterRequestId += 1;
  state.configurationRequestId += 1;
}

function clearCentralOverlay(message = "Waiting for an overlay for this experiment.") {
  centralOverlayImage.hidden = true;
  centralOverlayImage.removeAttribute("src");
  centralOverlayCaption.textContent = message;
  state.latestOverlayKey = null;
  state.lastOverlayRefreshAt = 0;
}

function renderMutationState() {
  renderExperiments();
  updateParameterDraftState();
  renderRuntimeControls();
}

function markSystemStatusUnavailable(message, label = "offline") {
  state.systemStatusFresh = false;
  state.controllerStatus = null;
  systemRefreshStatus.textContent = label;
  systemRefreshStatus.className = "status error";
  controllerLiveStatus.textContent = "unavailable";
  controllerLiveStatus.className = "status error";
  gsensorLiveStatus.textContent = "unconfirmed";
  gsensorLiveStatus.className = "status error";
  [gsensorLiveFrame, gsensorLiveImage, controllerLastFrame, controllerValidCount,
    controllerInvalidCount, controllerSeedCount, controllerLastSeedAt,
    controllerAdaptationStatus].forEach((element) => { element.textContent = "—"; });
  controllerLiveError.textContent = message;
  controllerLiveError.hidden = false;
  clearCentralOverlay("Status is unconfirmed. Refresh to view the current experiment overlay.");
  renderMutationState();
}

function applyUiMode(mode) {
  const resolved = mode === "development" ? "development" : "production";
  state.uiMode = resolved;
  document.documentElement.dataset.uiMode = resolved;
  renderRunConfiguration();
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

function runConfigurationLocked() {
  if (!state.systemStatusFresh || mutationInFlight()) {
    return true;
  }
  const status = currentExperiment()?.status;
  if (!status) {
    return false;
  }
  return !["created", "completed", "error"].includes(status);
}

function collectRunConfiguration() {
  return {
    run_type: runTypeSelect.value,
    controller_mode: controllerModeSelect.value,
    control_target: controlTargetSelect.value,
    adaptation_enabled: adaptationEnabledSelect.value === "true",
    adaptation_mode: adaptationModeSelect.value,
    growth_rate_source: growthRateSourceSelect.value,
  };
}

function runtimeRunActive() {
  const status = currentExperiment()?.status;
  return Boolean(status) && !["created", "completed", "error"].includes(status);
}

function runtimeReady() {
  const controller = state.controllerStatus;
  return runtimeRunActive() && currentExperiment()?.status !== "stopping"
    && state.systemStatusFresh && controller?.available
    && controller.current_run_id === state.currentRunId
    && controller.status === "running" && controller.runtime_controls?.supported;
}

function runtimeUnconfirmed(request = state.runtimeRequest) {
  return Boolean(request) && !["applied", "rejected"].includes(request.status);
}

function runtimeSessionKey(runId) { return `central-runtime:${runId}`; }

function rememberRuntimeRequest(request) {
  state.runtimeRequest = request;
  if (!request?.command?.run_id) return;
  try {
    if (runtimeUnconfirmed(request)) {
      sessionStorage.setItem(runtimeSessionKey(request.command.run_id), JSON.stringify(request));
    } else {
      sessionStorage.removeItem(runtimeSessionKey(request.command.run_id));
    }
  } catch (_) { /* Central also journals delivered requests; memory remains usable. */ }
}

function observeRuntimeRequest(remote) {
  let local = state.runtimeRequest;
  if (local?.command?.run_id !== state.currentRunId) {
    local = null;
    state.runtimeError = "";
    try { local = JSON.parse(sessionStorage.getItem(runtimeSessionKey(state.currentRunId))); } catch (_) {}
  }
  if (local?.status === "rejected" && remote?.command?.event_id !== local.command.event_id) {
    rememberRuntimeRequest(local); // Do not relabel a rejected edit using an older successful request.
  } else if (!runtimeUnconfirmed(local) || remote?.command?.event_id === local?.command?.event_id
      || (remote && runtimeUnconfirmed(remote))) {
    rememberRuntimeRequest(remote || local);
  } else {
    rememberRuntimeRequest(local);
  }
}

function renderAdaptiveSelection(mode, disabled) {
  const selected = adaptiveCombinations[mode] || [];
  adaptiveCheckboxes.forEach((box) => {
    box.checked = selected.includes(box.dataset.adaptiveParameter);
    box.disabled = disabled;
  });
  adaptationModeSelect.value = mode;
}

function renderSetpoints(configuration, disabled) {
  Object.entries(setpointInputs).forEach(([key, input]) => {
    if (document.activeElement !== input || input.dataset.dirty !== "true") {
      input.value = configuration?.[key] ?? "";
    }
    input.disabled = disabled;
  });
}

function renderSetpointExplanation(key) {
  const input = setpointInputs[key];
  const text = input.value.trim();
  const value = Number(text);
  // Do not interpret incomplete exponents, hex, Infinity or other non-decimal input.
  const decimal = /^[+]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text);
  const converted = value * (key === "sigma_set" ? 100 : 1e6);
  const element = document.querySelector(`#conversion-${key}`);
  element.textContent = decimal && value > 0 && Number.isFinite(converted)
    ? `${input.dataset.dirty === "true" ? "Editing preview: " : ""}${Number(converted.toPrecision(8))}${key === "sigma_set" ? "% relative supersaturation" : " μm/s"}`
    : "Enter a finite positive decimal value.";
}

function runtimeChangeDescription(entry, key) {
  const result = entry.result || {};
  const before = result.before || entry.before || {};
  const after = result.configuration || entry.command?.changes || {};
  const label = { control_target: "Control Target", adaptation_enabled: "Adaptation",
    adaptation_mode: "Adaptive Mode", sigma_set: "σ_set", G_set: "G_set" }[key];
  const display = value => value === undefined ? "unavailable" : String(value);
  let description = `${label}: ${display(before[key])} → ${display(after[key])}.`;
  if (key === "control_target" && after.control_target) {
    const selected = after.control_target === "G" ? "G_set" : "sigma_set";
    description += ` Using ${selected} = ${display(after[selected])}.`;
  }
  if ((key === "G_set" && after.control_target === "sigma")
      || (key === "sigma_set" && after.control_target === "G")) {
    description += " Standby value updated; control target unchanged.";
  }
  return description;
}

function renderConfigurationDetails(configuration, live) {
  const current = live ? configuration?.control_target : state.runConfiguration?.control_target;
  Object.keys(setpointInputs).forEach(key => {
    const target = key === "sigma_set" ? "sigma" : "G";
    const element = document.querySelector(`#role-${key}`);
    element.textContent = !current ? "Target status unconfirmed" : current === target
      ? (live ? "Current target value" : "Selected startup target value")
      : `Standby value — used after switching to ${target}`;
    element.classList.toggle("active", current === target);
    renderSetpointExplanation(key);
  });
  const entries = [...state.runtimeHistory];
  if (state.runtimeRequest?.command?.run_id === state.currentRunId) {
    const index = entries.findIndex(item => item.command.event_id === state.runtimeRequest.command.event_id);
    if (index >= 0) entries[index] = state.runtimeRequest;
    else if (!entries.length || runtimeUnconfirmed() || state.runtimeError) entries.unshift(state.runtimeRequest);
  }
  const keys = ["control_target", "sigma_set", "G_set", "adaptation_enabled", "adaptation_mode"];
  keys.forEach(key => {
    const element = document.querySelector(`#feedback-${key}`);
    const entry = live && entries.find(item => item.command?.run_id === state.currentRunId
      && Object.hasOwn(item.command?.changes || {}, key));
    element.textContent = "";
    element.classList.remove("error");
    if (!entry) return;
    const result = entry.result || {};
    const identity = result.event_id === entry.command.event_id && result.run_id === state.currentRunId;
    if (entry.status === "applied" && identity) {
      const saved = state.controllerStatus?.recovery?.status !== "persistence_error" && !entry.persistence_error;
      element.textContent = `${saved ? "Applied" : "Applied, but not durably saved"}: ${runtimeChangeDescription(entry, key)} Revision ${result.revision}; effective tick ${result.effective_tick ?? "unavailable"}.`;
      if (state.controllerStatus?.runtime_controls?.history_error) element.textContent += " History persistence incomplete.";
      if (entry.persistence_error) element.textContent += ` ${entry.persistence_error}`;
      element.classList.toggle("error", !saved);
    } else if (entry.status === "rejected") {
      element.textContent = `Rejected: ${result.reason || entry.transport_error || "Update was not accepted."}`;
      element.classList.add("error");
    } else {
      element.textContent = `${entry.status === "timeout" ? "Confirmation timed out. " : ""}Waiting for Controller confirmation.${entry.transport_error ? " " + entry.transport_error : ""}`;
    }
  });
  const panel = document.querySelector("#runtime-fitting");
  const status = state.controllerStatus;
  const adaptation = live && status?.available && status.current_run_id === state.currentRunId
    ? status.adaptation : null;
  if (!adaptation) {
    panel.textContent = "Fitting diagnostics unavailable. No confirmed live Controller data.";
    panel.classList.remove("error");
    return;
  }
  const fitting = adaptation.fitting;
  const modes = adaptiveCombinations[adaptation.mode]?.join(", ") || "unavailable";
  const states = { not_run: "Not run", waiting_for_samples: "Waiting for samples",
    fit_succeeded: "Last fit succeeded", fit_failed: "Last fit failed" };
  const value = field => fitting?.[field] ?? "unavailable";
  panel.textContent = `Adaptation ${adaptation.enabled ? "enabled" : "disabled"}. Selected parameters: ${modes}. `
    + (!adaptation.enabled ? "Parameter updates stopped; control continues with existing parameters. Historical fitting results: " : "")
    + (fitting ? `${states[fitting.last_status] || "Status unavailable"}. Samples ${value("sample_count")}/${value("minimum_samples")}; completed fits ${value("fit_count")}; failures ${value("failure_count")}. Last successful fit: ${fitting.last_success_at ? formatExperimentTime(fitting.last_success_at) : "unavailable"}.`
      + (fitting.last_failure_reason ? ` Last failure: ${fitting.last_failure_reason}.` : "")
      + (fitting.pause_reason ? ` Current reason: ${fitting.pause_reason}.` : "") : "Fitting diagnostics unavailable.");
  panel.classList.toggle("error", adaptation.enabled && fitting?.last_status === "fit_failed");
}

async function loadRuntimeHistory(older = false) {
  const runId = state.currentRunId;
  const message = document.querySelector("#runtime-history-status");
  const rows = document.querySelector("#runtime-history-rows");
  if (!runId || state.historyLoading) return;
  state.historyLoading = true;
  document.querySelector("#runtime-history-older").disabled = true;
  document.querySelector("#runtime-history-refresh").disabled = true;
  message.textContent = "Loading confirmed and pending changes…";
  try {
    const query = new URLSearchParams({ run_id: runId, limit: "30" });
    if (older && state.historyRunId === runId && state.historyCursor) query.set("before", state.historyCursor);
    const page = await fetchJson(`/api/operation/controller/runtime/history?${query}`);
    if (state.currentRunId !== runId) return;
    if (!older || state.historyRunId !== runId) rows.replaceChildren();
    state.historyRunId = runId;
    state.historyCursor = page.next_cursor;
    for (const entry of page.entries || []) {
      const result = entry.result || {};
      const tr = document.createElement("tr");
      const changes = Object.keys(entry.command.changes).map(key => runtimeChangeDescription(entry, key)).join(" ");
      const outcome = `${entry.status}; revision ${result.revision ?? "unavailable"}; effective tick ${result.effective_tick ?? "unavailable"}${result.reason ? "; " + result.reason : ""}${entry.persistence === "persistence_error" ? "; active state not durably saved" : ""}`;
      const sync = !page.grafana_enabled ? "Not configured" : (entry.grafana_sync === "synced" ? "Synced"
        : entry.grafana_sync === "not_applicable" ? "No change annotation" : `Incomplete: ${entry.grafana_error || entry.grafana_sync || "pending"}`);
      for (const text of [formatExperimentTime(entry.command.requested_at),
        result.applied_at ? `Applied: ${formatExperimentTime(result.applied_at)}`
          : result.processed_at ? `${result.source === "central" ? "Central decision" : "Controller processed"}: ${formatExperimentTime(result.processed_at)}; not an application timestamp`
            : "Not confirmed", changes, outcome, sync]) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.append(td);
      }
      rows.append(tr);
    }
    message.textContent = page.error || page.grafana_error || "Available recorded history only; missing legacy events are not reconstructed.";
    message.classList.toggle("error", Boolean(page.error || page.grafana_error));
    document.querySelector("#runtime-history-older").hidden = !page.next_cursor;
  } catch (error) {
    if (state.currentRunId === runId) { message.textContent = `History unavailable: ${error.message}`; message.classList.add("error"); }
  } finally {
    state.historyLoading = false;
    document.querySelector("#runtime-history-older").disabled = false;
    document.querySelector("#runtime-history-refresh").disabled = false;
  }
}

function renderLiveConfiguration() {
  const runtime = state.systemStatusFresh && state.controllerStatus?.available && state.controllerStatus?.current_run_id === state.currentRunId
    ? state.controllerStatus?.runtime_controls : null;
  const configuration = runtime?.configuration;
  runConfigurationControls.forEach((control) => { control.disabled = true; });
  adaptationEnableLabel.textContent = "Adaptation";
  const disabled = !runtimeReady() || mutationInFlight() || runtimeUnconfirmed();
  if (configuration) {
    controlTargetSelect.value = configuration.control_target;
    adaptationEnabledSelect.value = String(configuration.adaptation_enabled);
  } else {
    controlTargetSelect.value = "";
    adaptationEnabledSelect.value = "";
  }
  controlTargetSelect.disabled = disabled;
  adaptationEnabledSelect.disabled = disabled;
  renderAdaptiveSelection(configuration?.adaptation_mode || "", disabled);
  renderSetpoints(configuration, disabled);
  renderConfigurationDetails(configuration, true);
  const request = state.runtimeRequest;
  const timedOut = runtimeUnconfirmed() && Date.now() / 1000 - (request.created_at || 0) >= 30;
  retryRuntimeButton.hidden = !runtimeUnconfirmed();
  retryRuntimeButton.disabled = !runtimeReady() || mutationInFlight();
  let label = "live";
  let message = `Actual Controller settings · revision ${runtime?.revision ?? "—"}. Changes affect this run only.`;
  if (!runtimeReady()) {
    label = "unconfirmed";
    message = "Controller is unavailable, stopped or belongs to another run. Editing is locked.";
  } else if (state.runtimeInFlight || runtimeUnconfirmed()) {
    label = timedOut || request?.status === "timeout" ? "confirmation timeout" : "awaiting confirmation";
    message = `Request ${request?.command?.event_id || "…"}: Controller application is not yet confirmed. Retry uses the same event.`;
    if (request?.transport_error) message += ` ${request.transport_error}`;
  } else if (request?.status === "applied") {
    label = "applied";
    message += ` Last request applied at revision ${request.result.revision}, effective tick ${request.result.effective_tick}.`;
  } else if (request?.status === "rejected") {
    label = "rejected";
    message = request.result?.reason || "Controller rejected the update; prior settings remain.";
  }
  const persistenceFailed = runtimeReady() && state.controllerStatus?.recovery?.status === "persistence_error";
  if (persistenceFailed) {
    if (label === "applied" || label === "live") label = "active — not saved";
    message += " Controller state could not be saved. Active settings are not guaranteed to survive a restart; resolve the storage error before restarting.";
  }
  if (state.runtimeError) message += ` ${state.runtimeError}`;
  if (state.runtimeHistoryError) {
    if (label === "applied" || label === "live") label = "active — history incomplete";
    message += ` ${state.runtimeHistoryError} Controller settings remain authoritative; history may be incomplete.`;
  }
  runConfigurationStatus.textContent = label;
  runConfigurationStatus.className = ["rejected", "unconfirmed", "confirmation timeout"].includes(label) || state.runtimeError || persistenceFailed || state.runtimeHistoryError
    ? "status error" : (label === "applied" ? "status success" : "status running");
  runConfigurationMessage.textContent = message;
  runConfigurationMessage.classList.toggle("error", Boolean(state.runtimeError || state.runtimeHistoryError) || label === "rejected" || persistenceFailed);
}

function applyRunConfigurationPayload(payload) {
  if (!payload) {
    return;
  }
  state.runConfiguration = payload.configuration || state.runConfiguration;
  state.runConfigurationDefaults = payload.defaults || state.runConfigurationDefaults;
  state.runConfigurationUpdatedAt = payload.updated_at || null;
  state.target = state.runConfiguration?.control_target || state.target;
  renderRunConfiguration();
}

function renderRunConfiguration() {
  const configuration = state.runConfiguration;
  if (!configuration || !runConfigurationStatus) {
    return;
  }

  runTypeSelect.value = configuration.run_type;
  controllerModeSelect.value = configuration.controller_mode;
  controlTargetSelect.value = configuration.control_target;
  adaptationEnabledSelect.value = String(configuration.adaptation_enabled);
  adaptationModeSelect.value = configuration.adaptation_mode;
  growthRateSourceSelect.value = configuration.growth_rate_source;
  if (runtimeRunActive()) {
    renderLiveConfiguration();
    return;
  }
  adaptationEnableLabel.textContent = "Adaptation at Start";
  retryRuntimeButton.hidden = true;

  const locked = runConfigurationLocked();
  runConfigurationControls.forEach((control) => {
    control.disabled = state.runConfigurationInFlight || locked;
  });
  renderAdaptiveSelection(configuration.adaptation_mode, locked);
  renderSetpoints(state.params?.controller, locked || !state.params);
  renderConfigurationDetails(state.params?.controller, false);

  growthRateSourceSelect.querySelectorAll("[data-development-source]").forEach((option) => {
    const selected = option.value === configuration.growth_rate_source;
    option.hidden = state.uiMode !== "development" && !selected;
    option.disabled = state.uiMode !== "development";
  });

  if (state.runConfigurationInFlight) {
    runConfigurationStatus.textContent = "saving";
    runConfigurationStatus.className = "status idle";
    runConfigurationMessage.textContent = "Saving run configuration…";
    runConfigurationMessage.classList.remove("error");
    return;
  }
  if (state.runConfigurationError) {
    runConfigurationStatus.textContent = "error";
    runConfigurationStatus.className = "status error";
    runConfigurationMessage.textContent = state.runConfigurationError;
    runConfigurationMessage.classList.add("error");
    return;
  }
  if (state.runConfigurationRefreshError) {
    runConfigurationStatus.textContent = "saved; refresh pending";
    runConfigurationStatus.className = "status error";
    runConfigurationMessage.textContent = state.runConfigurationRefreshError;
    runConfigurationMessage.classList.add("error");
    return;
  }
  if (locked) {
    runConfigurationStatus.textContent = "locked";
    runConfigurationStatus.className = "status running";
    runConfigurationMessage.textContent = !state.systemStatusFresh
      ? "Refresh system status before changing the configuration."
      : (mutationInFlight() ? "Wait for the current operation to finish." : "Configuration is locked for the active run.");
    runConfigurationMessage.classList.remove("error");
    return;
  }

  const savedTime = formatParameterTime(state.runConfigurationUpdatedAt);
  runConfigurationStatus.textContent = savedTime ? "saved" : "defaults";
  runConfigurationStatus.className = "status success";
  runConfigurationMessage.textContent = savedTime
    ? `Configuration saved at ${savedTime}. Changes are applied automatically.`
    : "Using the default run configuration. Changes are saved automatically.";
  runConfigurationMessage.classList.remove("error");
}

async function loadRunConfiguration() {
  const requestId = ++state.configurationRequestId;
  const payload = await fetchJson("/api/run-configuration");
  if (requestId !== state.configurationRequestId) return payload;
  applyRunConfigurationPayload(payload);
  return payload;
}

async function saveRunConfiguration() {
  if (!state.runConfiguration || state.runConfigurationInFlight || runConfigurationLocked()) {
    renderRunConfiguration();
    return null;
  }
  const previous = { ...state.runConfiguration };
  const draft = collectRunConfiguration();
  const expectedRunId = state.currentRunId;
  invalidatePendingReads();
  state.runConfiguration = draft;
  state.runConfigurationInFlight = true;
  state.runConfigurationError = null;
  state.runConfigurationRefreshError = null;
  renderMutationState();
  let saved = false;
  try {
    const payload = await fetchJson("/api/run-configuration", {
      method: "PUT",
      body: JSON.stringify({ ...draft, expected_run_id: expectedRunId }),
    });
    applyRunConfigurationPayload(payload);
    saved = true;
    await loadOperationState({ updateRunConfiguration: false });
    return payload;
  } catch (error) {
    if (!saved) state.runConfiguration = previous;
    if (saved) {
      state.runConfigurationRefreshError = `Configuration saved, but preview refresh failed: ${error.message}`;
    } else {
      state.runConfigurationError = error.message;
    }
    return null;
  } finally {
    state.runConfigurationInFlight = false;
    renderMutationState();
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

function renderForm(form, params, sectionName) {
  form.innerHTML = "";
  const entries = Object.entries(params)
    .map(([key, value], index) => ({ key, value, index, meta: state.paramMeta[key] || {} }))
    .filter(({ meta }) => {
      const uiMeta = meta.ui || {};
      return uiMeta.visible !== false;
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

    const title = document.createElement("h4");
    title.className = "param-group-title";
    title.textContent = group.section;
    wrapper.appendChild(title);

    group.items.forEach(({ key, value, meta }) => {
      const field = fieldTemplate.content.firstElementChild.cloneNode(true);
      const label = field.querySelector(".field-key");
      const badges = field.querySelector(".field-badges");
      const description = field.querySelector(".field-description");
      const expression = field.querySelector(".field-expression");
      const depends = field.querySelector(".field-depends");
      let input = field.querySelector(".field-input");
      const modifiedBadge = field.querySelector(".field-modified");
      const resetButton = field.querySelector(".field-reset");
      const defaultValue = state.params?.defaults?.[sectionName]?.[key];

      label.textContent = meta.label || key;
      description.textContent = meta.description || "";
      description.classList.toggle("is-empty", !meta.description);

      expression.textContent = meta.expression ? `Expression: ${meta.expression}` : "";
      expression.classList.toggle("is-empty", !meta.expression);

      depends.textContent = Array.isArray(meta.depends_on) && meta.depends_on.length > 0
        ? `Depends on: ${meta.depends_on.join(", ")}`
        : "";
      depends.classList.toggle("is-empty", !(Array.isArray(meta.depends_on) && meta.depends_on.length > 0));

      const badgeItems = [];
      if (meta.unit) {
        badgeItems.push(`unit: ${meta.unit}`);
      }
      if (meta.kind) {
        badgeItems.push(meta.kind);
      } else if (meta.derived) {
        badgeItems.push("derived");
      }
      if (Array.isArray(meta.publish_to) && meta.publish_to.length > 0) {
        badgeItems.push(`publish: ${meta.publish_to.join(", ")}`);
      }
      badges.innerHTML = badgeItems.map((item) => `<span class="field-badge">${item}</span>`).join("");
      badges.classList.toggle("is-empty", badgeItems.length === 0);

      if (Array.isArray(meta.choices) && meta.choices.length > 0) {
        const select = document.createElement("select");
        select.className = input.className;
        meta.choices.forEach((choice) => {
          const option = document.createElement("option");
          option.value = choice;
          option.textContent = choice;
          select.appendChild(option);
        });
        input.replaceWith(select);
        input = select;
      }
      input.dataset.key = key;
      input.dataset.section = sectionName;
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

    form.appendChild(wrapper);
  });
}

function collectForm(form) {
  const data = {};
  form.querySelectorAll(".field-input").forEach((input) => {
    data[input.dataset.key] = parseFieldValue(input.value);
  });
  return data;
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
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 15000);
  try {
    const response = await fetch(cacheBustedUrl(url, options), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      ...options,
      signal: options.signal || abort.signal,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = payload?.detail;
      const message = Array.isArray(detail)
        ? detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg || "Invalid value"}`).join("; ")
        : (typeof detail === "string" ? detail : `Request failed (HTTP ${response.status}).`);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (payload === null) throw new Error("The server returned an unreadable response.");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Request timed out. Refresh status before retrying the operation.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function loadParams() {
  const requestId = ++state.parameterRequestId;
  const payload = await fetchJson("/api/params");
  if (requestId !== state.parameterRequestId) return;
  state.params = payload;
  state.paramMeta = state.params.meta || {};
  state.parameterError = null;
  state.parameterRefreshError = null;
  renderParameterForms();
}

function renderParameterForms() {
  renderForm(sharedForm, state.params.shared, "shared");
  renderForm(gsensorForm, state.params.gsensor, "gsensor");
  renderForm(controllerForm, state.params.controller, "controller");
  updateParameterDraftState();
}

function collectParameterDraft() {
  return {
    shared: collectForm(sharedForm),
    gsensor: collectForm(gsensorForm),
    controller: collectForm(controllerForm),
  };
}

function parameterPayloadFromDraft() {
  return {
    version: state.params?.version || 1,
    ...collectParameterDraft(),
    expected_run_id: state.currentRunId,
  };
}

function parametersLocked() {
  if (!state.systemStatusFresh || mutationInFlight()) return true;
  const status = currentExperiment()?.status;
  return Boolean(status && !["created", "completed", "error"].includes(status));
}

function updateParameterDraftState() {
  if (!state.params) {
    return;
  }
  let unsavedCount = 0;
  let modifiedCount = 0;
  const locked = parametersLocked();
  document.querySelectorAll(".field-input").forEach((input) => {
    const section = input.dataset.section;
    const key = input.dataset.key;
    const value = parseFieldValue(input.value);
    const savedValue = state.params?.[section]?.[key];
    const defaultValue = state.params?.defaults?.[section]?.[key];
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
  } else if (state.parameterRefreshError) {
    kind = "error";
    message = state.parameterRefreshError;
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

function formatExperimentTime(value) {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  const pad = (part) => String(part).padStart(2, "0");
  return [
    parsed.getFullYear(),
    pad(parsed.getMonth() + 1),
    pad(parsed.getDate()),
  ].join("-") + ` ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

function displayNameForExperiment(experiment) {
  if (!experiment) {
    return "No experiment selected";
  }
  if (experiment.label) {
    return experiment.label;
  }
  const createdAt = formatExperimentTime(experiment.created_at);
  return createdAt === "—" ? "Untitled Experiment" : `Experiment ${createdAt}`;
}

function setExperimentMessage(message, { error = false } = {}) {
  experimentMessage.textContent = message;
  experimentMessage.classList.toggle("error", error);
}

function currentExperiment() {
  return state.experiments.find((item) => item.run_id === state.currentRunId) || null;
}

function renderExperiments() {
  const current = currentExperiment();
  const hasCurrent = Boolean(current);
  const status = current?.status || null;
  const completedAfterStop = current
    && state.lastRenderedRunId === current.run_id
    && state.lastRenderedExperimentStatus === "stopping"
    && status === "completed";
  const isTerminal = ["completed", "error"].includes(status);
  const canCreate = !hasCurrent || isTerminal;
  const canStart = ["created", "starting"].includes(status);
  const selectionRetryRequired = status === "created"
    && state.selectionRetryRunId === current?.run_id;
  const canRetrySelection = status === "created" && (selectionRetryRequired
    || (state.gsensorStatusRunId !== current?.run_id && state.selectionSentRunId !== current?.run_id));
  const canEnd = hasCurrent && !isTerminal;
  const action = state.experimentActionName;
  const blocked = !state.systemStatusFresh || mutationInFlight();
  if (state.lastRenderedRunId !== current?.run_id) {
    clearCentralOverlay();
    document.querySelector("#runtime-history-rows").replaceChildren();
    document.querySelector("#runtime-history-status").textContent = "Refresh history for the selected run.";
    document.querySelector("#runtime-history-older").hidden = true;
    state.historyRunId = null;
    state.historyCursor = null;
  }

  experimentStatus.textContent = current?.status || "not selected";
  experimentStatus.className = `status ${current?.status || "idle"}`;
  experimentDisplayName.textContent = displayNameForExperiment(current);
  experimentRunId.textContent = current?.run_id || "—";
  experimentCreatedAt.textContent = formatExperimentTime(current?.created_at);
  experimentStartedAt.textContent = formatExperimentTime(current?.started_at);
  experimentEndedAt.textContent = formatExperimentTime(current?.ended_at);
  cameraSavePath.value = current?.camera_save_path || "";

  copyCameraPathButton.disabled = !hasCurrent || state.experimentActionInFlight;
  newExperimentButton.disabled = !canCreate || blocked;
  startExperimentButton.disabled = !canStart
    || blocked
    || selectionRetryRequired
    || state.runConfigurationInFlight
    || Boolean(state.runConfigurationError);
  endExperimentButton.disabled = !canEnd || blocked;
  retrySelectionButton.hidden = !canRetrySelection;
  retrySelectionButton.disabled = !canRetrySelection || blocked;
  retrySelectionButton.textContent = action === "select" ? "Resending selection…" : "Retry Selection";
  newExperimentLabel.disabled = !canCreate || blocked;

  newExperimentButton.textContent = action === "create" ? "Creating…" : "Create Experiment";
  startExperimentButton.textContent = action === "start"
    ? "Starting…"
    : (status === "starting" ? "Retry Start" : "Start Experiment");
  endExperimentButton.textContent = action === "end"
    ? "Ending…"
    : (status === "stopping" ? "Retry End" : "End Experiment");

  if (completedAfterStop) {
    setExperimentMessage(`Completed ${displayNameForExperiment(current)}. Gsensor finalization is complete.`);
  }
  state.lastRenderedRunId = current?.run_id || null;
  state.lastRenderedExperimentStatus = status;
  renderRunConfiguration();
}

async function loadExperiments() {
  const requestId = ++state.experimentRequestId;
  const payload = await fetchJson("/api/experiments");
  if (requestId !== state.experimentRequestId) return;
  if (state.currentRunId !== (payload.current_run_id || null)) {
    state.systemStatusFresh = false;
  }
  state.experiments = payload.experiments || [];
  state.currentRunId = payload.current_run_id || null;
  renderExperiments();
  updateParameterDraftState();
}

function updateCentralOverlay(runId, frameSeq, { final = false, force = false } = {}) {
  if (!runId || !frameSeq) {
    return;
  }
  const kind = final ? "final" : "latest";
  const key = `${runId}:${kind}:${frameSeq}`;
  const now = Date.now();
  if (!force && (key === state.latestOverlayKey || now - state.lastOverlayRefreshAt < 3000)) {
    return;
  }
  centralOverlayImage.src = `/api/experiments/${encodeURIComponent(runId)}/overlay/${kind}?frame_seq=${encodeURIComponent(frameSeq)}&_=${now}`;
  centralOverlayImage.hidden = false;
  centralOverlayCaption.textContent = `${final ? "Final" : "Latest"} overlay · frame ${frameSeq}`;
  state.latestOverlayKey = key;
  state.lastOverlayRefreshAt = now;
}

function renderSystemStatus(payload) {
  const gsensor = payload.gsensor || {};
  const controller = payload.controller || {};
  const current = currentExperiment();
  const gsensorMatchesRun = Boolean(current?.run_id)
    && gsensor.last_status?.run_id === current.run_id;
  const gsensorStatus = gsensorMatchesRun ? gsensor.last_status : {};
  state.controllerStatus = controller;
  state.runtimeHistory = payload.runtime_history || [];
  state.runtimeHistoryError = payload.runtime_history_error || "";
  observeRuntimeRequest(payload.runtime_request || null);

  if (state.seedActionRunId && state.seedActionRunId !== current?.run_id) {
    state.seedActionRunId = null;
    state.pendingSeedEventId = null;
    state.seedActionMessage = "";
    state.seedActionError = false;
  }

  systemRefreshStatus.textContent = "online";
  systemRefreshStatus.className = "status running";
  gsensorLiveStatus.textContent = gsensorStatus.status || "waiting for this run";
  gsensorLiveStatus.className = gsensorStatus.status === "error"
    ? "status error"
    : (gsensorStatus.status ? "status running" : "status idle");
  gsensorLiveFrame.textContent = gsensorStatus.frame_seq ?? "—";
  gsensorLiveImage.textContent = gsensorStatus.image_name || "—";
  gsensorMessageCount.textContent = String(gsensor.message_count || 0);
  gsensorReceivedAt.textContent = formatExperimentTime(gsensorMatchesRun ? gsensor.received_at : null);
  gsensorLiveError.textContent = gsensorStatus.error || gsensor.consumer_error || "";
  gsensorLiveError.hidden = !gsensorLiveError.textContent;

  controllerLiveStatus.textContent = controller.status || "unavailable";
  controllerLiveStatus.className = controller.available
    ? (controller.status === "error" ? "status error" : "status running")
    : "status error";
  controllerRunId.textContent = controller.current_run_id || "—";
  controllerLastFrame.textContent = controller.last_frame_seq ?? "—";
  controllerValidCount.textContent = String(controller.sample_counts?.valid || 0);
  controllerInvalidCount.textContent = String(controller.sample_counts?.invalid || 0);
  controllerSeedCount.textContent = String(controller.seed_events?.count || 0);
  controllerLastSeedAt.textContent = formatExperimentTime(controller.seed_events?.last?.added_at);
  const adaptation = controller.adaptation || {};
  controllerAdaptationStatus.textContent = adaptation.enabled ? "enabled" : "disabled";
  controllerAdaptationMode.textContent = adaptation.mode || "E_A";
  const fitting = adaptation.fitting;
  adaptationActionMessage.textContent = fitting
    ? `Last fitting status: ${fitting.last_status}; mode ${fitting.last_mode || "—"}; tick ${fitting.last_tick ?? "—"}. Samples ${fitting.sample_count}/${fitting.minimum_samples}; completed fits ${fitting.fit_count}; failures ${fitting.failure_count}.${fitting.pause_reason ? " " + fitting.pause_reason : ""}`
    : "Fitting diagnostics unavailable.";
  adaptationActionMessage.classList.toggle("error", fitting?.last_status === "fit_failed");
  const previousConnectionError = !controller.error
    && typeof controller.last_error === "string"
    && controller.last_error.startsWith("RabbitMQ consumer error:")
    && controller.consumer?.status === "consuming"
    && controller.consumer?.thread_alive === true;
  controllerLiveError.textContent = previousConnectionError
    ? `Previous connection error; connection currently reported active. ${controller.last_error}`
    : (controller.error || controller.last_error || "");
  controllerLiveError.hidden = !controllerLiveError.textContent;

  const lastSeedEventId = controller.seed_events?.last?.event_id || null;
  const lastControllerEventId = controller.last_message?.payload?.event_id || null;
  if (state.pendingSeedEventId && lastSeedEventId === state.pendingSeedEventId) {
    state.pendingSeedEventId = null;
    state.seedActionMessage = `Seed addition recorded at ${formatExperimentTime(controller.seed_events.last.added_at)}.`;
    state.seedActionError = false;
  } else if (
    state.pendingSeedEventId
    && lastControllerEventId === state.pendingSeedEventId
    && controller.last_message_result?.accepted === false
  ) {
    state.pendingSeedEventId = null;
    state.seedActionMessage = controller.last_message_result.reason || "Controller rejected the Add Seed event.";
    state.seedActionError = true;
  }


  renderRuntimeControls();

  const frameSeq = Number(gsensorStatus.frame_seq || 0);
  if (current?.run_id && frameSeq > 0) {
    updateCentralOverlay(current.run_id, frameSeq, {
      final: current.status === "completed",
    });
  }
}

function renderRuntimeControls() {
  const current = currentExperiment();
  const controller = state.controllerStatus || {};
  const adaptation = controller.adaptation || {};
  const activeExperiment = current && [
    "starting",
    "waiting_for_initial_image",
    "initializing",
    "measuring",
  ].includes(current.status);
  const controllerReady = state.systemStatusFresh && controller.available
    && controller.status === "running"
    && controller.current_run_id === current?.run_id;
  const runtimeActionInFlight = mutationInFlight()
    || Boolean(state.pendingSeedEventId);
  addSeedButton.disabled = runtimeActionInFlight || !activeExperiment || !controllerReady;
  addSeedButton.textContent = state.seedActionInFlight ? "Recording..." : "Add Seed";
  seedActionMessage.textContent = state.seedActionMessage;
  seedActionMessage.classList.toggle("error", state.seedActionError);
  renderRunConfiguration();
}

async function loadSystemStatus({ forceOverlay = false } = {}) {
  const requestId = ++state.experimentRequestId;
  state.systemStatusInFlight += 1;
  let payload;
  try {
    payload = await fetchJson("/api/system/status");
  } catch (error) {
    if (requestId !== state.experimentRequestId) return null;
    markSystemStatusUnavailable(error.message);
    throw error;
  } finally {
    state.systemStatusInFlight -= 1;
  }
  if (requestId !== state.experimentRequestId) return null;
  const experiments = payload.experiments || {};
  if ((experiments.current_run_id || null) !== (payload.current_experiment?.run_id || null)
    || (experiments.current_run_id && !(experiments.experiments || []).some(
      (item) => item.run_id === experiments.current_run_id,
    ))) {
    markSystemStatusUnavailable("Experiment identity is inconsistent. Refresh before operating.", "unconfirmed");
    throw new Error("Experiment identity is inconsistent. Refresh before operating.");
  }
  if (state.currentRunId !== (experiments.current_run_id || null)) {
    state.operationRequestId += 1;
    state.parameterRequestId += 1;
    state.configurationRequestId += 1;
  }
  state.experiments = experiments.experiments || [];
  state.currentRunId = experiments.current_run_id || null;
  state.gsensorStatusRunId = payload.gsensor?.last_status?.run_id || null;
  state.systemStatusFresh = true;
  renderExperiments();
  updateParameterDraftState();
  renderSystemStatus(payload);
  if (forceOverlay) {
    const status = payload.gsensor?.last_status || {};
    const frameSeq = Number(status.frame_seq || 0);
    if (payload.current_experiment?.run_id && frameSeq > 0
      && status.run_id === payload.current_experiment.run_id) {
      updateCentralOverlay(payload.current_experiment.run_id, frameSeq, {
        final: payload.current_experiment.status === "completed",
        force: true,
      });
    }
  }
  return payload;
}

async function runExperimentAction(actionName, action, successMessage) {
  if (!state.systemStatusFresh || mutationInFlight()) return null;
  const previousRunId = state.currentRunId;
  invalidatePendingReads();
  state.experimentActionInFlight = true;
  state.experimentActionName = actionName;
  state.systemStatusFresh = false;
  systemRefreshStatus.textContent = "refreshing";
  systemRefreshStatus.className = "status idle";
  setExperimentMessage("");
  renderMutationState();
  let result = null;
  try {
    result = await action();
    if (["create", "select"].includes(actionName)) {
      state.selectionSentRunId = result.run_id;
      if (state.selectionRetryRunId === result.run_id) state.selectionRetryRunId = null;
    }
    await loadSystemStatus();
    setExperimentMessage(successMessage(result));
    return result;
  } catch (error) {
    if (result) {
      setExperimentMessage(`${successMessage(result)} Status refresh failed: ${error.message} Confirm the current status before retrying.`, { error: true });
      return result;
    }
    try {
      await loadSystemStatus();
    } catch (reloadError) {
      setExperimentMessage(`${error.message} Reload failed: ${reloadError.message}`, { error: true });
      return null;
    }
    const current = currentExperiment();
    if (current?.status === "created" && (
      (actionName === "create" && current.run_id !== previousRunId)
      || (actionName === "select" && current.run_id === previousRunId)
    )) {
      state.selectionRetryRunId = current.run_id;
      setExperimentMessage(`Experiment ${displayNameForExperiment(current)} is created, but its selection notification failed. Use Retry Selection before Start. ${error.message}`, { error: true });
    } else {
      setExperimentMessage(error.message, { error: true });
    }
    return null;
  } finally {
    state.experimentActionInFlight = false;
    state.experimentActionName = null;
    renderMutationState();
  }
}

async function createExperiment() {
  if (newExperimentButton.disabled) return null;
  const label = newExperimentLabel.value.trim();
  const expectedRunId = state.currentRunId;
  const result = await runExperimentAction(
    "create",
    () => fetchJson("/api/experiments", {
      method: "POST",
      body: JSON.stringify({ label: label || null, expected_run_id: expectedRunId }),
    }),
    (experiment) => `Created ${displayNameForExperiment(experiment)}. Review the run configuration and parameters, point the camera software to the path shown above, then start the experiment.`,
  );
  if (result) {
    newExperimentLabel.value = "";
    try {
      await loadOperationState();
    } catch (error) {
      setExperimentMessage(`Experiment created, but configuration refresh failed: ${error.message}`, { error: true });
    }
  }
  return result;
}

async function finishCurrentExperiment() {
  const current = currentExperiment();
  if (!current || endExperimentButton.disabled) {
    return;
  }
  const confirmed = window.confirm(
    current.status === "stopping"
      ? "Retry sending End to Gsensor and Controller for this experiment?"
      : "End the current experiment? New images will no longer be processed.",
  );
  if (!confirmed) {
    return;
  }
  const result = await runExperimentAction(
    "end",
    () => fetchJson(`/api/experiments/${encodeURIComponent(current.run_id)}/finish`, {
      method: "POST",
      body: JSON.stringify({ expected_run_id: current.run_id }),
    }),
    (experiment) => experiment.status === "completed"
      ? `Ended ${displayNameForExperiment(experiment)}.`
      : `End sent for ${displayNameForExperiment(experiment)}. Waiting for Gsensor to save the final result and confirm completion.`,
  );
  const refreshed = currentExperiment();
  if (result && refreshed?.run_id === current.run_id && refreshed?.status === "completed"
    && state.systemStatusFresh) {
    setExperimentMessage(`Completed ${displayNameForExperiment(refreshed)}. Gsensor finalization is complete.`);
  }
}

async function retryExperimentSelection() {
  const current = currentExperiment();
  if (!current || current.status !== "created" || retrySelectionButton.disabled) return null;
  return runExperimentAction(
    "select",
    () => fetchJson(`/api/experiments/${encodeURIComponent(current.run_id)}/select`, {
      method: "POST",
      body: JSON.stringify({ expected_run_id: current.run_id }),
    }),
    (experiment) => `Selection notification resent for ${displayNameForExperiment(experiment)}. You can now start this experiment.`,
  );
}

async function addSeed() {
  const current = currentExperiment();
  if (!current || addSeedButton.disabled) {
    return;
  }
  const confirmed = window.confirm(
    "Confirm that seed has physically been added to the reactor? This will record the event in Controller.",
  );
  if (!confirmed) {
    return;
  }

  state.seedActionInFlight = true;
  invalidatePendingReads();
  state.seedActionRunId = current.run_id;
  state.seedActionMessage = "Recording seed addition...";
  state.seedActionError = false;
  renderMutationState();
  let sent = false;
  try {
    const result = await fetchJson("/api/operation/controller/add-seed", {
      method: "POST",
      body: JSON.stringify({ expected_run_id: current.run_id }),
    });
    sent = true;
    if (state.currentRunId !== current.run_id) return;
    state.pendingSeedEventId = result.event.event_id;
    state.seedActionMessage = "Add Seed sent. Waiting for Controller confirmation...";
  } catch (error) {
    if (state.currentRunId === current.run_id) {
      state.pendingSeedEventId = null;
      state.seedActionMessage = `${error.message} Check Controller status before retrying Add Seed.`;
      state.seedActionError = true;
    }
  } finally {
    state.seedActionInFlight = false;
    renderMutationState();
    try {
      await loadSystemStatus();
    } catch (error) {
      if (state.currentRunId === current.run_id) {
        state.seedActionMessage = `${sent ? "Add Seed was sent; confirmation is still pending." : state.seedActionMessage} Status refresh failed: ${error.message} Do not send Add Seed again until confirmed.`;
        state.seedActionError = true;
        renderRuntimeControls();
      }
    }
  }
}


async function submitRuntimeUpdate(changes, retryCommand = null) {
  if (!runtimeReady() || mutationInFlight() || (runtimeUnconfirmed() && !retryCommand)) {
    renderRunConfiguration();
    return;
  }
  const command = retryCommand || {
    run_id: state.currentRunId, event_id: crypto.randomUUID(),
    expected_revision: state.controllerStatus.runtime_controls.revision,
    changes, requested_at: new Date().toISOString(),
  };
  if (command.run_id !== state.currentRunId) return;
  const request = retryCommand ? state.runtimeRequest : {
    command, status: "pending", created_at: Date.now() / 1000, result: null,
    before: { ...state.controllerStatus.runtime_controls.configuration },
  };
  rememberRuntimeRequest(request);
  state.runtimeError = "";
  state.runtimeInFlight = true;
  invalidatePendingReads();
  renderMutationState();
  try {
    const response = await fetchJson("/api/operation/controller/runtime", {
      method: "POST", body: JSON.stringify(command),
    });
    if (state.currentRunId === command.run_id) rememberRuntimeRequest(response.runtime_request);
  } catch (error) {
    if (state.currentRunId === command.run_id) {
      state.runtimeError = error.message;
      if ([400, 403, 404, 409, 422].includes(error.status)) {
        rememberRuntimeRequest({ ...request, status: "rejected", result: { reason: error.message } });
      } else {
        rememberRuntimeRequest({ ...request, transport_error: "HTTP delivery is unconfirmed." });
      }
    }
  } finally {
    state.runtimeInFlight = false;
    try { await loadSystemStatus(); } catch (error) { state.runtimeError = error.message; }
    renderMutationState();
  }
}

async function commitSetpoint(key, input) {
  if (input.disabled || input.dataset.dirty !== "true") return;
  input.dataset.dirty = "false"; // Enter followed by blur is one operation.
  const text = input.value.trim();
  const value = Number(text);
  if (!/^[+]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text) || !Number.isFinite(value) || value <= 0) {
    input.setCustomValidity("Enter a finite number greater than zero.");
    input.reportValidity();
    if (runtimeRunActive()) state.runtimeError = "Setpoint rejected: enter a finite positive number.";
    else state.runConfigurationError = "Setpoint rejected: enter a finite positive number.";
    renderRunConfiguration();
    return;
  }
  input.setCustomValidity("");
  if (runtimeRunActive()) {
    const revision = String(state.controllerStatus?.runtime_controls?.revision);
    if (input.dataset.editRunId !== state.currentRunId || input.dataset.editRevision !== revision) {
      state.runtimeError = "Settings changed while you were typing. Review the current values and edit again.";
      renderRunConfiguration();
      return;
    }
    if (state.controllerStatus.runtime_controls.configuration[key] === value) return;
    await submitRuntimeUpdate({ [key]: value });
  } else {
    if (runConfigurationLocked() || !state.params) return;
    if (state.parameterUnsavedCount) {
      state.runConfigurationError = "Save or discard parameter drawer edits before changing a setpoint here.";
      renderRunConfiguration();
      return;
    }
    if (state.params.controller[key] === value) return;
    const draft = {
      version: state.params.version, expected_run_id: state.currentRunId,
      shared: { ...state.params.shared }, gsensor: { ...state.params.gsensor },
      controller: { ...state.params.controller, [key]: value },
    };
    state.parameterActionInFlight = true;
    state.runConfigurationError = null;
    invalidatePendingReads();
    renderMutationState();
    try {
      state.params = await fetchJson("/api/params", { method: "POST", body: JSON.stringify(draft) });
      renderParameterForms();
      await loadOperationState();
    } catch (error) {
      state.runConfigurationError = error.message;
    } finally {
      state.parameterActionInFlight = false;
      renderMutationState();
    }
  }
}

async function copyCameraPath() {
  const path = cameraSavePath.value;
  if (!path) {
    return;
  }
  try {
    await navigator.clipboard.writeText(path);
  } catch (error) {
    cameraSavePath.select();
    const copied = document.execCommand("copy");
    cameraSavePath.setSelectionRange(0, 0);
    if (!copied) {
      setExperimentMessage("Could not copy the camera path. Select and copy it manually.", { error: true });
      return;
    }
  }
  setExperimentMessage("Camera save path copied.");
}

async function loadOperationState({ updateRunConfiguration = true } = {}) {
  const requestId = ++state.operationRequestId;
  const payload = await fetchJson("/api/operation/state");
  if (requestId !== state.operationRequestId) return;
  state.runConfigurationRefreshError = null;
  state.parameterRefreshError = null;
  state.target = payload.target;
  if (updateRunConfiguration && !state.runConfigurationInFlight) {
    applyRunConfigurationPayload(payload.run_configuration);
  }
  derivedPreview.textContent = JSON.stringify(payload.preview, null, 2);
  renderParameterStatus(state.parameterUnsavedCount);
}

async function saveParams() {
  if (saveParamsButton.disabled || !state.systemStatusFresh || mutationInFlight()) return null;
  const draft = parameterPayloadFromDraft();
  invalidatePendingReads();
  state.parameterActionInFlight = true;
  state.parameterError = null;
  state.parameterRefreshError = null;
  renderMutationState();
  let saved = false;
  try {
    state.params = await fetchJson("/api/params", {
      method: "POST",
      body: JSON.stringify(draft),
    });
    saved = true;
    renderParameterForms();
    await loadOperationState();
    return state.params;
  } catch (error) {
    if (saved) state.parameterRefreshError = `Parameters saved, but preview refresh failed: ${error.message}`;
    else state.parameterError = error.message;
    renderMutationState();
    return null;
  } finally {
    state.parameterActionInFlight = false;
    renderMutationState();
  }
}

async function resetParamsToDefaults() {
  if (resetParamsButton.disabled || !state.systemStatusFresh || mutationInFlight()) return null;
  const expectedRunId = state.currentRunId;
  invalidatePendingReads();
  state.parameterActionInFlight = true;
  state.parameterError = null;
  state.parameterRefreshError = null;
  renderMutationState();
  let saved = false;
  try {
    state.params = await fetchJson("/api/params/reset", {
      method: "POST",
      body: JSON.stringify({ expected_run_id: expectedRunId }),
    });
    saved = true;
    renderParameterForms();
    await loadOperationState();
  } catch (error) {
    if (saved) state.parameterRefreshError = `Defaults restored, but preview refresh failed: ${error.message}`;
    else state.parameterError = error.message;
    renderMutationState();
    return null;
  } finally {
    state.parameterActionInFlight = false;
    renderMutationState();
  }
}

async function startExperimentCommand() {
  if (!state.systemStatusFresh || mutationInFlight() || startExperimentButton.disabled) return null;
  if (state.runConfigurationInFlight) {
    setExperimentMessage("Wait for the run configuration to finish saving before starting.", { error: true });
    return null;
  }
  if (state.runConfigurationError) {
    setExperimentMessage("Resolve the run-configuration save error before starting.", { error: true });
    return null;
  }
  commandStatus.textContent = "saving parameters";
  commandStatus.className = "status idle";
  state.parameterActionInFlight = true;
  state.experimentActionInFlight = true;
  state.experimentActionName = "start";
  state.systemStatusFresh = false;
  systemRefreshStatus.textContent = "refreshing";
  systemRefreshStatus.className = "status idle";
  state.parameterError = null;
  const draft = parameterPayloadFromDraft();
  invalidatePendingReads();
  setExperimentMessage("");
  renderMutationState();
  let accepted = null;
  try {
    const payload = await fetchJson("/api/operation/experiment/start", {
      method: "POST",
      body: JSON.stringify(draft),
    });
    accepted = payload;
    state.params = payload.parameters;
    renderParameterForms();
    commandResult.textContent = JSON.stringify(payload, null, 2);
    commandStatus.textContent = "start accepted";
    commandStatus.className = "status success";
    await loadSystemStatus();
    setExperimentMessage(`Start accepted for ${displayNameForExperiment(payload.experiment)}. Service status is shown below.`);
    return payload;
  } catch (error) {
    if (accepted) {
      commandStatus.textContent = "start accepted";
      commandStatus.className = "status success";
      const message = `Start accepted for ${displayNameForExperiment(accepted.experiment)}, but status refresh failed: ${error.message} Confirm the current status before retrying.`;
      setExperimentMessage(message, { error: true });
      commandResult.textContent = message;
      return accepted;
    }
    try {
      await Promise.all([loadParams(), loadSystemStatus(), loadRunConfiguration()]);
    } catch (reloadError) {
      commandResult.textContent = `${error.message}\nReload failed: ${reloadError.message}`;
    }
    state.parameterError = error.message;
    setExperimentMessage(error.message, { error: true });
    if (!commandResult.textContent.includes("Reload failed:")) {
      commandResult.textContent = error.message;
    }
    commandStatus.textContent = "error";
    commandStatus.className = "status error";
    return null;
  } finally {
    state.parameterActionInFlight = false;
    state.experimentActionInFlight = false;
    state.experimentActionName = null;
    renderMutationState();
  }
}

toggleParametersButton.addEventListener("click", () => {
  setDrawerOpen(!state.drawerOpen);
});

resetParamsButton.addEventListener("click", async () => {
  await resetParamsToDefaults();
});

saveParamsButton.addEventListener("click", async () => {
  await saveParams();
});

refreshPreviewButton.addEventListener("click", async (event) => {
  event.preventDefault();
  event.stopPropagation();
  try {
    await loadOperationState();
  } catch (error) {
    commandStatus.textContent = "error";
    commandStatus.className = "status error";
    commandResult.textContent = `Preview refresh failed: ${error.message}`;
  }
});

newExperimentButton.addEventListener("click", async () => {
  await createExperiment();
});

newExperimentLabel.addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    await createExperiment();
  }
});

startExperimentButton.addEventListener("click", async () => {
  await startExperimentCommand();
});

retrySelectionButton.addEventListener("click", async () => {
  await retryExperimentSelection();
});

endExperimentButton.addEventListener("click", async () => {
  await finishCurrentExperiment();
});

addSeedButton.addEventListener("click", async () => {
  await addSeed();
});


copyCameraPathButton.addEventListener("click", async () => {
  await copyCameraPath();
});

centralRefreshOverlay.addEventListener("click", () => {
  loadSystemStatus({ forceOverlay: true }).catch((error) => {
    systemRefreshStatus.textContent = error.message;
    systemRefreshStatus.className = "status error";
  });
});

centralOverlayImage.addEventListener("error", () => {
  centralOverlayImage.hidden = true;
  centralOverlayCaption.textContent = "Overlay is not available yet.";
});

runConfigurationControls.forEach((control) => {
  control.addEventListener("change", () => {
    if (runtimeRunActive()) {
      const key = {
        "control-target": "control_target", "adaptation-enabled": "adaptation_enabled",
        "adaptation-mode": "adaptation_mode",
      }[control.id];
      if (key) submitRuntimeUpdate({
        [key]: key === "adaptation_enabled" ? control.value === "true" : control.value,
      });
      else renderRunConfiguration();
      return;
    }
    saveRunConfiguration().catch((error) => {
      state.runConfigurationError = error.message;
      state.runConfigurationInFlight = false;
      renderRunConfiguration();
    });
  });
});

adaptiveCheckboxes.forEach((box) => {
  box.addEventListener("change", () => {
    const selected = adaptiveCheckboxes.filter((item) => item.checked).map((item) => item.dataset.adaptiveParameter);
    const mode = Object.keys(adaptiveCombinations).find((name) =>
      adaptiveCombinations[name].length === selected.length
      && adaptiveCombinations[name].every((name) => selected.includes(name)));
    if (!mode) {
      box.checked = true;
      if (runtimeRunActive()) state.runtimeError = "Keep at least one parameter selected; use Adaptation to stop fitting.";
      else state.runConfigurationError = "Keep at least one parameter selected.";
      renderRunConfiguration();
      return;
    }
    adaptationModeSelect.value = mode;
    adaptationModeSelect.dispatchEvent(new Event("change"));
  });
});

Object.entries(setpointInputs).forEach(([key, input]) => {
  input.addEventListener("focus", () => {
    input.dataset.editRevision = String(state.controllerStatus?.runtime_controls?.revision);
    input.dataset.editRunId = state.currentRunId || "";
  });
  input.addEventListener("input", () => {
    if (input.dataset.dirty !== "true") {
      input.dataset.editRevision = String(state.controllerStatus?.runtime_controls?.revision);
      input.dataset.editRunId = state.currentRunId || "";
    }
    input.dataset.dirty = "true";
    input.setCustomValidity("");
    renderSetpointExplanation(key);
  });
  input.addEventListener("blur", () => { commitSetpoint(key, input); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); commitSetpoint(key, input); }
  });
});
retryRuntimeButton.addEventListener("click", () => {
  if (runtimeUnconfirmed()) submitRuntimeUpdate(null, state.runtimeRequest.command);
});
document.querySelector("#runtime-history").addEventListener("toggle", event => {
  if (event.target.open) loadRuntimeHistory();
});
document.querySelector("#runtime-history-refresh").addEventListener("click", () => loadRuntimeHistory());
document.querySelector("#runtime-history-older").addEventListener("click", () => loadRuntimeHistory(true));

async function bootstrap() {
  await loadUiConfig();
  await loadRunConfiguration();
  await loadParams();
  await loadOperationState();
  try {
    await loadSystemStatus();
  } catch (error) {
    setExperimentMessage(error.message, { error: true });
  }
}

bootstrap().catch((error) => {
  commandResult.textContent = error.message;
  commandStatus.textContent = "error";
  commandStatus.className = "status error";
});

setInterval(() => {
  if (!mutationInFlight() && !state.systemStatusInFlight && !document.hidden) {
    Promise.all([loadOperationState(), loadSystemStatus()]).catch((error) => {
      commandResult.textContent = error.message;
      commandStatus.textContent = "error";
      commandStatus.className = "status error";
    });
  }
}, 2000);

window.addEventListener("focus", () => {
  if (!mutationInFlight() && state.parameterUnsavedCount === 0) {
    loadParams().catch((error) => {
      state.parameterError = error.message;
      updateParameterDraftState();
    });
  }
  if (!mutationInFlight()) {
    loadOperationState().catch((error) => {
      commandResult.textContent = error.message;
      commandStatus.textContent = "error";
      commandStatus.className = "status error";
    });
  }
  if (!mutationInFlight() && !state.systemStatusInFlight) {
    loadSystemStatus().catch((error) => {
      setExperimentMessage(error.message, { error: true });
    });
  }
});
