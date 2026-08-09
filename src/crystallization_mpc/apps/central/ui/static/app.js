const state = {
  params: null,
  paramMeta: {},
  operationMeta: [],
  operationState: {},
  target: "sigma",
  drawerOpen: false,
  actionInFlight: false,
  operationStateSignature: "",
  experiments: [],
  currentRunId: null,
  experimentActionInFlight: false,
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
const operationSections = document.querySelector("#operation-sections");
const fieldTemplate = document.querySelector("#param-field-template");
const operationItemTemplate = document.querySelector("#operation-item-template");
const experimentStatus = document.querySelector("#experiment-status");
const experimentRunId = document.querySelector("#experiment-run-id");
const experimentCurrentLabel = document.querySelector("#experiment-current-label");
const experimentCreatedAt = document.querySelector("#experiment-created-at");
const experimentStartedAt = document.querySelector("#experiment-started-at");
const experimentEndedAt = document.querySelector("#experiment-ended-at");
const cameraSavePath = document.querySelector("#camera-save-path");
const copyCameraPathButton = document.querySelector("#copy-camera-path");
const newExperimentLabel = document.querySelector("#new-experiment-label");
const newExperimentButton = document.querySelector("#new-experiment");
const endExperimentButton = document.querySelector("#end-experiment");
const experimentHistory = document.querySelector("#experiment-history");
const selectExperimentButton = document.querySelector("#select-experiment");
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
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function setExperimentMessage(message, { error = false } = {}) {
  experimentMessage.textContent = message;
  experimentMessage.classList.toggle("error", error);
}

function currentExperiment() {
  return state.experiments.find((item) => item.run_id === state.currentRunId) || null;
}

function experimentOptionLabel(experiment) {
  const label = experiment.label ? ` — ${experiment.label}` : "";
  return `${experiment.run_id}${label} (${experiment.status})`;
}

function renderExperiments({ selectedRunId = null } = {}) {
  const current = currentExperiment();
  const hasCurrent = Boolean(current);
  const isTerminal = current && ["completed", "error"].includes(current.status);

  experimentStatus.textContent = current?.status || "not selected";
  experimentStatus.className = `status ${current?.status || "idle"}`;
  experimentRunId.textContent = current?.run_id || "No experiment selected";
  experimentCurrentLabel.textContent = current?.label || "—";
  experimentCreatedAt.textContent = formatExperimentTime(current?.created_at);
  experimentStartedAt.textContent = formatExperimentTime(current?.started_at);
  experimentEndedAt.textContent = formatExperimentTime(current?.ended_at);
  cameraSavePath.value = current?.camera_save_path || "";

  copyCameraPathButton.disabled = !hasCurrent || state.experimentActionInFlight;
  endExperimentButton.disabled = !hasCurrent || isTerminal || state.experimentActionInFlight;
  newExperimentButton.disabled = state.experimentActionInFlight;
  newExperimentLabel.disabled = state.experimentActionInFlight;

  const previousSelection = selectedRunId || experimentHistory.value || state.currentRunId;
  experimentHistory.innerHTML = "";
  if (state.experiments.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No experiments available";
    experimentHistory.appendChild(option);
  } else {
    state.experiments.forEach((experiment) => {
      const option = document.createElement("option");
      option.value = experiment.run_id;
      option.textContent = experimentOptionLabel(experiment);
      experimentHistory.appendChild(option);
    });
    const availableSelection = state.experiments.some(
      (experiment) => experiment.run_id === previousSelection,
    );
    experimentHistory.value = availableSelection
      ? previousSelection
      : state.experiments[0].run_id;
  }

  experimentHistory.disabled = state.experiments.length === 0 || state.experimentActionInFlight;
  selectExperimentButton.disabled = state.experiments.length === 0 || state.experimentActionInFlight;
}

async function loadExperiments(options = {}) {
  const payload = await fetchJson("/api/experiments");
  state.experiments = payload.experiments || [];
  state.currentRunId = payload.current_run_id || null;
  renderExperiments(options);
  updateParameterDraftState();
  if (state.operationMeta.length > 0) {
    renderOperationSections();
  }
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
  if (state.operationMeta.length > 0) {
    renderOperationSections();
  }
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

async function runExperimentAction(action, successMessage) {
  state.experimentActionInFlight = true;
  setExperimentMessage("");
  renderExperiments();
  try {
    const result = await action();
    await loadExperiments({ selectedRunId: result?.run_id || null });
    setExperimentMessage(successMessage(result));
    return result;
  } catch (error) {
    setExperimentMessage(error.message, { error: true });
    return null;
  } finally {
    state.experimentActionInFlight = false;
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
    () => fetchJson("/api/experiments", {
      method: "POST",
      body: JSON.stringify({ label: label || null }),
    }),
    (experiment) => `Created ${experiment.run_id}. Point the camera software to the path shown above.`,
  );
  if (result) {
    newExperimentLabel.value = "";
    await loadParams();
    await loadOperationState();
  }
  return result;
}

async function selectExperiment() {
  const runId = experimentHistory.value;
  if (!runId) {
    return;
  }
  await runExperimentAction(
    () => fetchJson(`/api/experiments/${encodeURIComponent(runId)}/select`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
    (experiment) => `Selected ${experiment.run_id}.`,
  );
}

async function finishCurrentExperiment() {
  const current = currentExperiment();
  if (!current) {
    return;
  }
  const confirmed = window.confirm(`End experiment ${current.run_id}? This action cannot be undone.`);
  if (!confirmed) {
    return;
  }
  await runExperimentAction(
    () => fetchJson(`/api/experiments/${encodeURIComponent(current.run_id)}/finish`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
    (experiment) => `Ended ${experiment.run_id}.`,
  );
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

async function loadOperationMeta() {
  const payload = await fetchJson("/api/operation/meta");
  state.operationMeta = payload.sections || [];
}

function renderOperationControl(item) {
  const control = document.createElement("div");
  control.className = "operation-control";
  const currentValue = state.operationState[item.key];

  if (item.key === "experiment_active") {
    return renderExperimentLifecycleControl();
  }

  if (item.kind === "list") {
    const select = document.createElement("select");
    select.disabled = state.actionInFlight;
    (item.options || []).forEach((option) => {
      const optionEl = document.createElement("option");
      optionEl.value = option;
      optionEl.textContent = option;
      optionEl.selected = option === currentValue;
      select.appendChild(optionEl);
    });
    select.addEventListener("change", async (event) => {
      try {
        state.actionInFlight = true;
        select.disabled = true;
        await updateOperationValue(item.key, event.target.value);
      } finally {
        state.actionInFlight = false;
        await loadOperationState({ forceRender: true });
      }
    });
    control.appendChild(select);
    return control;
  }

  return control;
}

function renderExperimentLifecycleControl() {
  const control = document.createElement("div");
  control.className = "operation-control operation-control-actions";
  const current = currentExperiment();
  const canStart = current?.status === "created";
  const canStop = current && !["created", "completed", "error"].includes(current.status);

  const startButton = document.createElement("button");
  startButton.className = "primary";
  startButton.textContent = "Start experiment";
  startButton.disabled = state.actionInFlight || !canStart;
  startButton.addEventListener("click", async () => {
    try {
      state.actionInFlight = true;
      startButton.disabled = true;
      await startExperimentCommand();
    } finally {
      state.actionInFlight = false;
      await loadOperationState({ forceRender: true });
    }
  });

  const stopButton = document.createElement("button");
  stopButton.className = "ghost";
  stopButton.textContent = "Stop experiment";
  stopButton.disabled = state.actionInFlight || !canStop;
  stopButton.addEventListener("click", async () => {
    try {
      state.actionInFlight = true;
      stopButton.disabled = true;
      await stopExperimentCommand();
    } finally {
      state.actionInFlight = false;
      await loadOperationState({ forceRender: true });
    }
  });

  control.appendChild(startButton);
  control.appendChild(stopButton);
  return control;
}

function renderOperationSections() {
  operationSections.innerHTML = "";
  state.operationMeta.forEach((section) => {
    const wrapper = document.createElement("section");
    wrapper.className = "operation-section";

    const title = document.createElement("h4");
    title.className = "operation-section-title";
    title.textContent = section.title;
    wrapper.appendChild(title);

    (section.items || []).forEach((item) => {
      const row = operationItemTemplate.content.firstElementChild.cloneNode(true);
      row.querySelector(".operation-label").textContent = item.label;
      row.querySelector(".operation-key").textContent = item.key;
      row.querySelector(".operation-control").replaceWith(renderOperationControl(item));
      wrapper.appendChild(row);
    });

    operationSections.appendChild(wrapper);
  });
}

async function loadOperationState({ forceRender = false } = {}) {
  const payload = await fetchJson("/api/operation/state");
  state.target = payload.target;
  const nextOperationState = payload.state || {};
  const nextSignature = JSON.stringify(nextOperationState);
  const shouldRender = forceRender || nextSignature !== state.operationStateSignature;
  state.operationState = nextOperationState;
  state.operationStateSignature = nextSignature;
  derivedPreview.textContent = JSON.stringify(payload.preview, null, 2);
  if (shouldRender) {
    renderOperationSections();
  }
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

async function updateOperationValue(key, value) {
  await fetchJson("/api/operation/value", {
    method: "POST",
    body: JSON.stringify({ key, value }),
  });
}

async function startExperimentCommand() {
  commandStatus.textContent = "saving parameters";
  commandStatus.className = "status idle";
  state.parameterActionInFlight = true;
  state.parameterError = null;
  updateParameterDraftState();
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
    await loadExperiments({ selectedRunId: payload.experiment?.run_id || null });
  } catch (error) {
    state.parameterError = error.message;
    commandResult.textContent = error.message;
    commandStatus.textContent = "error";
    commandStatus.className = "status error";
    return null;
  } finally {
    state.parameterActionInFlight = false;
    updateParameterDraftState();
  }
}

async function stopExperimentCommand() {
  commandStatus.textContent = "stopping experiment";
  commandStatus.className = "status idle";
  try {
    const payload = await fetchJson("/api/operation/experiment/stop", {
      method: "POST",
      body: JSON.stringify({}),
    });
    commandResult.textContent = JSON.stringify(payload, null, 2);
    commandStatus.textContent = "stopped";
    commandStatus.className = "status success";
    await loadExperiments({ selectedRunId: payload.experiment?.run_id || null });
  } catch (error) {
    commandResult.textContent = error.message;
    commandStatus.textContent = "error";
    commandStatus.className = "status error";
    return null;
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

selectExperimentButton.addEventListener("click", async () => {
  await selectExperiment();
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

async function bootstrap() {
  await loadParams();
  await loadOperationMeta();
  await loadOperationState({ forceRender: true });
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
  if (!state.actionInFlight && !document.hidden) {
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
  if (!state.actionInFlight) {
    loadOperationState({ forceRender: true }).catch((error) => {
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
