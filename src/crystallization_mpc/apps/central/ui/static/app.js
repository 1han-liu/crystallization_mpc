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
  drawerOpen: false,
  experiments: [],
  currentRunId: null,
  lastRenderedRunId: null,
  lastRenderedExperimentStatus: null,
  experimentActionInFlight: false,
  experimentActionName: null,
  parameterActionInFlight: false,
  parameterError: null,
  parameterUnsavedCount: 0,
  latestOverlayKey: null,
  lastOverlayRefreshAt: 0,
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

const runConfigurationControls = [
  runTypeSelect,
  controllerModeSelect,
  controlTargetSelect,
  adaptationEnabledSelect,
  adaptationModeSelect,
  growthRateSourceSelect,
];

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

  const locked = runConfigurationLocked();
  runConfigurationControls.forEach((control) => {
    control.disabled = state.runConfigurationInFlight || locked;
  });
  adaptationModeSelect.disabled = state.runConfigurationInFlight
    || locked
    || !configuration.adaptation_enabled;

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
  if (locked) {
    runConfigurationStatus.textContent = "locked";
    runConfigurationStatus.className = "status running";
    runConfigurationMessage.textContent = "Configuration is locked for the active run.";
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
  const payload = await fetchJson("/api/run-configuration");
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
  state.runConfiguration = draft;
  state.runConfigurationInFlight = true;
  state.runConfigurationError = null;
  renderRunConfiguration();
  try {
    const payload = await fetchJson("/api/run-configuration", {
      method: "PUT",
      body: JSON.stringify(draft),
    });
    applyRunConfigurationPayload(payload);
    await loadOperationState({ updateRunConfiguration: false });
    return payload;
  } catch (error) {
    state.runConfiguration = previous;
    state.runConfigurationError = error.message;
    return null;
  } finally {
    state.runConfigurationInFlight = false;
    renderRunConfiguration();
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
      const input = field.querySelector(".field-input");
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

async function loadParams() {
  state.params = await fetchJson("/api/params");
  state.paramMeta = state.params.meta || {};
  state.parameterError = null;
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
  };
}

function parametersLocked() {
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
  const canStart = status === "created";
  const canEnd = hasCurrent && !isTerminal && status !== "stopping";
  const action = state.experimentActionName;

  experimentStatus.textContent = current?.status || "not selected";
  experimentStatus.className = `status ${current?.status || "idle"}`;
  experimentDisplayName.textContent = displayNameForExperiment(current);
  experimentRunId.textContent = current?.run_id || "—";
  experimentCreatedAt.textContent = formatExperimentTime(current?.created_at);
  experimentStartedAt.textContent = formatExperimentTime(current?.started_at);
  experimentEndedAt.textContent = formatExperimentTime(current?.ended_at);
  cameraSavePath.value = current?.camera_save_path || "";

  copyCameraPathButton.disabled = !hasCurrent || state.experimentActionInFlight;
  newExperimentButton.disabled = !canCreate || state.experimentActionInFlight;
  startExperimentButton.disabled = !canStart || state.experimentActionInFlight;
  endExperimentButton.disabled = !canEnd || state.experimentActionInFlight;
  newExperimentLabel.disabled = !canCreate || state.experimentActionInFlight;

  newExperimentButton.textContent = action === "create" ? "Creating…" : "Create Experiment";
  startExperimentButton.textContent = action === "start" ? "Starting…" : "Start Experiment";
  endExperimentButton.textContent = action === "end" ? "Ending…" : "End Experiment";

  if (completedAfterStop) {
    setExperimentMessage(`Completed ${displayNameForExperiment(current)}. Gsensor finalization is complete.`);
  }
  state.lastRenderedRunId = current?.run_id || null;
  state.lastRenderedExperimentStatus = status;
  renderRunConfiguration();
}

async function loadExperiments() {
  const payload = await fetchJson("/api/experiments");
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
  const gsensorStatus = gsensor.last_status || {};
  const controller = payload.controller || {};
  const current = payload.current_experiment || null;

  systemRefreshStatus.textContent = "online";
  systemRefreshStatus.className = "status running";
  gsensorLiveStatus.textContent = gsensorStatus.status || "no status";
  gsensorLiveStatus.className = gsensorStatus.status === "error"
    ? "status error"
    : (gsensorStatus.status ? "status running" : "status idle");
  gsensorLiveFrame.textContent = gsensorStatus.frame_seq ?? "—";
  gsensorLiveImage.textContent = gsensorStatus.image_name || "—";
  gsensorMessageCount.textContent = String(gsensor.message_count || 0);
  gsensorReceivedAt.textContent = formatExperimentTime(gsensor.received_at);
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
  controllerLiveError.textContent = controller.error || "";
  controllerLiveError.hidden = !controllerLiveError.textContent;

  const frameSeq = Number(gsensorStatus.frame_seq || 0);
  if (current?.run_id && frameSeq > 0) {
    updateCentralOverlay(current.run_id, frameSeq, {
      final: current.status === "completed",
    });
  }
}

async function loadSystemStatus({ forceOverlay = false } = {}) {
  const payload = await fetchJson("/api/system/status");
  const experiments = payload.experiments || {};
  state.experiments = experiments.experiments || [];
  state.currentRunId = experiments.current_run_id || null;
  renderExperiments();
  updateParameterDraftState();
  renderSystemStatus(payload);
  if (forceOverlay) {
    const status = payload.gsensor?.last_status || {};
    const frameSeq = Number(status.frame_seq || 0);
    if (payload.current_experiment?.run_id && frameSeq > 0) {
      updateCentralOverlay(payload.current_experiment.run_id, frameSeq, {
        final: payload.current_experiment.status === "completed",
        force: true,
      });
    }
  }
  return payload;
}

async function runExperimentAction(actionName, action, successMessage) {
  state.experimentActionInFlight = true;
  state.experimentActionName = actionName;
  setExperimentMessage("");
  renderExperiments();
  try {
    const result = await action();
    await loadExperiments();
    setExperimentMessage(successMessage(result));
    return result;
  } catch (error) {
    setExperimentMessage(error.message, { error: true });
    return null;
  } finally {
    state.experimentActionInFlight = false;
    state.experimentActionName = null;
    renderExperiments();
  }
}

async function createExperiment() {
  if (state.parameterUnsavedCount > 0) {
    const confirmed = window.confirm(
      "Creating a new experiment resets the parameter draft to defaults. Discard unsaved changes?",
    );
    if (!confirmed) {
      return null;
    }
  }
  const label = newExperimentLabel.value.trim();
  const result = await runExperimentAction(
    "create",
    () => fetchJson("/api/experiments", {
      method: "POST",
      body: JSON.stringify({ label: label || null }),
    }),
    (experiment) => `Created ${displayNameForExperiment(experiment)}. Point the camera software to the path shown above.`,
  );
  if (result) {
    newExperimentLabel.value = "";
    await loadParams();
    await loadOperationState();
  }
  return result;
}

async function finishCurrentExperiment() {
  const current = currentExperiment();
  if (!current) {
    return;
  }
  const confirmed = window.confirm(
    "End the current experiment? New images will no longer be processed.",
  );
  if (!confirmed) {
    return;
  }
  const result = await runExperimentAction(
    "end",
    () => fetchJson(`/api/experiments/${encodeURIComponent(current.run_id)}/finish`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
    (experiment) => experiment.status === "completed"
      ? `Ended ${displayNameForExperiment(experiment)}.`
      : `End requested for ${displayNameForExperiment(experiment)}. Waiting for Gsensor to finish saving results.`,
  );
  const refreshed = currentExperiment();
  if (result && refreshed?.status === "completed") {
    setExperimentMessage(`Completed ${displayNameForExperiment(refreshed)}. Gsensor finalization is complete.`);
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
  const payload = await fetchJson("/api/operation/state");
  state.target = payload.target;
  if (updateRunConfiguration && !state.runConfigurationInFlight) {
    applyRunConfigurationPayload(payload.run_configuration);
  }
  derivedPreview.textContent = JSON.stringify(payload.preview, null, 2);
}

async function saveParams() {
  state.parameterActionInFlight = true;
  state.parameterError = null;
  updateParameterDraftState();
  try {
    state.params = await fetchJson("/api/params", {
      method: "POST",
      body: JSON.stringify(parameterPayloadFromDraft()),
    });
    renderParameterForms();
    await loadOperationState();
    return state.params;
  } catch (error) {
    state.parameterError = error.message;
    updateParameterDraftState();
    return null;
  } finally {
    state.parameterActionInFlight = false;
    updateParameterDraftState();
  }
}

async function resetParamsToDefaults() {
  state.parameterActionInFlight = true;
  state.parameterError = null;
  updateParameterDraftState();
  try {
    state.params = await fetchJson("/api/params/reset", {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderParameterForms();
    await loadOperationState();
  } catch (error) {
    state.parameterError = error.message;
    updateParameterDraftState();
    return null;
  } finally {
    state.parameterActionInFlight = false;
    updateParameterDraftState();
  }
}

async function startExperimentCommand() {
  commandStatus.textContent = "saving parameters";
  commandStatus.className = "status idle";
  state.parameterActionInFlight = true;
  state.experimentActionInFlight = true;
  state.experimentActionName = "start";
  state.parameterError = null;
  setExperimentMessage("");
  updateParameterDraftState();
  renderExperiments();
  try {
    const payload = await fetchJson("/api/operation/experiment/start", {
      method: "POST",
      body: JSON.stringify(parameterPayloadFromDraft()),
    });
    state.params = payload.parameters;
    renderParameterForms();
    commandResult.textContent = JSON.stringify(payload, null, 2);
    commandStatus.textContent = "started";
    commandStatus.className = "status success";
    await loadExperiments();
    setExperimentMessage(`Started ${displayNameForExperiment(payload.experiment)}. Waiting for the first image.`);
    return payload;
  } catch (error) {
    state.parameterError = error.message;
    setExperimentMessage(error.message, { error: true });
    commandResult.textContent = error.message;
    commandStatus.textContent = "error";
    commandStatus.className = "status error";
    return null;
  } finally {
    state.parameterActionInFlight = false;
    state.experimentActionInFlight = false;
    state.experimentActionName = null;
    updateParameterDraftState();
    renderExperiments();
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
  await loadOperationState();
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

endExperimentButton.addEventListener("click", async () => {
  await finishCurrentExperiment();
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
    saveRunConfiguration().catch((error) => {
      state.runConfigurationError = error.message;
      state.runConfigurationInFlight = false;
      renderRunConfiguration();
    });
  });
});

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
  if (!state.experimentActionInFlight && !document.hidden) {
    Promise.all([loadOperationState(), loadSystemStatus()]).catch((error) => {
      commandResult.textContent = error.message;
      commandStatus.textContent = "error";
      commandStatus.className = "status error";
      systemRefreshStatus.textContent = "offline";
      systemRefreshStatus.className = "status error";
    });
  }
}, 2000);

window.addEventListener("focus", () => {
  if (!state.parameterActionInFlight && state.parameterUnsavedCount === 0) {
    loadParams().catch((error) => {
      state.parameterError = error.message;
      updateParameterDraftState();
    });
  }
  if (!state.experimentActionInFlight) {
    loadOperationState().catch((error) => {
      commandResult.textContent = error.message;
      commandStatus.textContent = "error";
      commandStatus.className = "status error";
    });
  }
  if (!state.experimentActionInFlight) {
    loadSystemStatus().catch((error) => {
      setExperimentMessage(error.message, { error: true });
    });
  }
});
