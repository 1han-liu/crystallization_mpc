const state = {
  params: null,
  paramMeta: {},
  target: "sigma",
  drawerOpen: false,
};

const shell = document.querySelector(".shell");
const toggleParametersButton = document.querySelector("#toggle-parameters");
const targetSelect = document.querySelector("#target-select");
const publishButton = document.querySelector("#publish-button");
const publishStatus = document.querySelector("#publish-status");
const publishResult = document.querySelector("#publish-result");
const derivedPreview = document.querySelector("#derived-preview");
const reloadParamsButton = document.querySelector("#reload-params");
const saveParamsButton = document.querySelector("#save-params");
const refreshPreviewButton = document.querySelector("#refresh-preview");
const sharedForm = document.querySelector("#shared-form");
const gsensorForm = document.querySelector("#gsensor-form");
const controllerForm = document.querySelector("#controller-form");
const fieldTemplate = document.querySelector("#param-field-template");

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

function renderForm(form, params) {
  form.innerHTML = "";
  Object.entries(params).forEach(([key, value]) => {
    const meta = state.paramMeta[key] || {};
    const uiMeta = meta.ui || {};
    if (uiMeta.visible === false) {
      return;
    }

    const field = fieldTemplate.content.firstElementChild.cloneNode(true);
    const label = field.querySelector(".field-key");
    const badges = field.querySelector(".field-badges");
    const description = field.querySelector(".field-description");
    const input = field.querySelector(".field-input");
    label.textContent = key;
    description.textContent = meta.description || "";
    description.classList.toggle("is-empty", !meta.description);

    const badgeItems = [];
    if (meta.unit) {
      badgeItems.push(`unit: ${meta.unit}`);
    }
    if (meta.derived) {
      badgeItems.push("derived");
    }
    if (Array.isArray(meta.publish_to) && meta.publish_to.length > 0) {
      badgeItems.push(`publish: ${meta.publish_to.join(", ")}`);
    }
    badges.innerHTML = badgeItems.map((item) => `<span class="field-badge">${item}</span>`).join("");
    badges.classList.toggle("is-empty", badgeItems.length === 0);

    input.dataset.key = key;
    input.value = formatFieldValue(value);
    form.appendChild(field);
  });
}

function collectForm(form) {
  const data = {};
  form.querySelectorAll(".field-input").forEach((input) => {
    data[input.dataset.key] = parseFieldValue(input.value);
  });
  return data;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
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
  renderForm(sharedForm, state.params.shared);
  renderForm(gsensorForm, state.params.gsensor);
  renderForm(controllerForm, state.params.controller);
}

async function loadOperationState() {
  const payload = await fetchJson("/api/operation/state");
  state.target = payload.target;
  targetSelect.value = payload.target;
  derivedPreview.textContent = JSON.stringify(payload.preview, null, 2);
}

async function saveParams() {
  const payload = {
    version: state.params?.version || 1,
    shared: collectForm(sharedForm),
    gsensor: collectForm(gsensorForm),
    controller: collectForm(controllerForm),
  };
  await fetchJson("/api/params", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.params = payload;
  await loadOperationState();
}

async function updateTarget(target) {
  await fetchJson("/api/operation/target", {
    method: "POST",
    body: JSON.stringify({ target }),
  });
  state.target = target;
  await loadOperationState();
}

async function publishParams() {
  publishStatus.textContent = "sending";
  publishStatus.className = "status idle";
  try {
    const payload = await fetchJson("/api/operation/publish", {
      method: "POST",
      body: JSON.stringify({}),
    });
    publishResult.textContent = JSON.stringify(payload, null, 2);
    publishStatus.textContent = "success";
    publishStatus.className = "status success";
  } catch (error) {
    publishResult.textContent = error.message;
    publishStatus.textContent = "error";
    publishStatus.className = "status error";
  }
}

toggleParametersButton.addEventListener("click", () => {
  setDrawerOpen(!state.drawerOpen);
});

targetSelect.addEventListener("change", async (event) => {
  await updateTarget(event.target.value);
});

reloadParamsButton.addEventListener("click", async () => {
  await loadParams();
});

saveParamsButton.addEventListener("click", async () => {
  await saveParams();
});

refreshPreviewButton.addEventListener("click", async () => {
  await loadOperationState();
});

publishButton.addEventListener("click", async () => {
  await publishParams();
});

async function bootstrap() {
  await loadParams();
  await loadOperationState();
}

bootstrap().catch((error) => {
  publishResult.textContent = error.message;
  publishStatus.textContent = "error";
  publishStatus.className = "status error";
});
