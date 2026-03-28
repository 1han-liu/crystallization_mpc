const state = {
  params: null,
  paramMeta: {},
  operationMeta: [],
  operationState: {},
  target: "sigma",
  drawerOpen: false,
};

const shell = document.querySelector(".shell");
const toggleParametersButton = document.querySelector("#toggle-parameters");
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
const operationSections = document.querySelector("#operation-sections");
const fieldTemplate = document.querySelector("#param-field-template");
const operationItemTemplate = document.querySelector("#operation-item-template");

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
      input.value = formatFieldValue(value);
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

async function loadOperationMeta() {
  const payload = await fetchJson("/api/operation/meta");
  state.operationMeta = payload.sections || [];
}

function renderOperationControl(item) {
  const control = document.createElement("div");
  control.className = "operation-control";
  const currentValue = state.operationState[item.key];

  if (item.kind === "list") {
    const select = document.createElement("select");
    (item.options || []).forEach((option) => {
      const optionEl = document.createElement("option");
      optionEl.value = option;
      optionEl.textContent = option;
      optionEl.selected = option === currentValue;
      select.appendChild(optionEl);
    });
    select.addEventListener("change", async (event) => {
      await updateOperationValue(item.key, event.target.value);
    });
    control.appendChild(select);
    return control;
  }

  const button = document.createElement("button");
  button.className = item.kind === "action_push" ? "ghost" : "primary";

  if (item.kind === "action_toggle") {
    const labels = item.labels || [item.label, item.label];
    button.textContent = currentValue ? labels[1] : labels[0];
    button.addEventListener("click", async () => {
      await triggerOperationAction(item.key);
    });
    control.appendChild(button);
    return control;
  }

  button.textContent = item.label;
  button.addEventListener("click", async () => {
    await triggerOperationAction(item.key);
  });
  control.appendChild(button);
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

async function loadOperationState() {
  const payload = await fetchJson("/api/operation/state");
  state.target = payload.target;
  state.operationState = payload.state || {};
  derivedPreview.textContent = JSON.stringify(payload.preview, null, 2);
  renderOperationSections();
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

async function updateOperationValue(key, value) {
  await fetchJson("/api/operation/value", {
    method: "POST",
    body: JSON.stringify({ key, value }),
  });
  await loadOperationState();
}

async function triggerOperationAction(key) {
  await fetchJson("/api/operation/action", {
    method: "POST",
    body: JSON.stringify({ key }),
  });
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
  await loadOperationMeta();
  await loadOperationState();
}

bootstrap().catch((error) => {
  publishResult.textContent = error.message;
  publishStatus.textContent = "error";
  publishStatus.className = "status error";
});
