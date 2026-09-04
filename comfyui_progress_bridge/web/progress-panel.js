import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
  PANEL_STORAGE_KEY,
  clampPanelPosition,
  readPanelPreferences,
  writePanelPreferences,
} from "./panel-preferences.mjs";
import { createState, reduceEnvelope, viewModel } from "./progress-state.mjs";

const LEGACY_COLLAPSED_KEY = "comfy-progress-bridge-collapsed";

function browserStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readCollapsed() {
  try {
    return localStorage.getItem(LEGACY_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed) {
  try {
    localStorage.setItem(LEGACY_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Restricted storage must not prevent the panel from working.
  }
}

function readPreferences() {
  const storage = browserStorage();
  let hasCurrentSettings = false;
  try {
    hasCurrentSettings = storage?.getItem?.(PANEL_STORAGE_KEY) !== null;
  } catch {
    // The safe reader below supplies defaults.
  }
  const preferences = readPanelPreferences(storage);
  return hasCurrentSettings ? preferences : { ...preferences, collapsed: readCollapsed() };
}

function addStyles() {
  if (document.getElementById("comfy-progress-bridge-styles")) return;
  const style = document.createElement("style");
  style.id = "comfy-progress-bridge-styles";
  style.textContent = `
    #comfy-progress-bridge-panel {
      --cpb-accent: #6c8eff;
      --cpb-panel: #202020;
      --cpb-card: #272b37;
      --cpb-text: #f4f6fc;
      --cpb-muted: #aab1c3;
      position: fixed; top: 58px; right: 16px; z-index: 10000; width: 310px;
      box-sizing: border-box; color: var(--cpb-text); background: var(--cpb-panel);
      border: 1px solid color-mix(in srgb, var(--cpb-accent) 45%, #555);
      border-radius: 10px; box-shadow: 0 8px 28px #0008; backdrop-filter: blur(8px);
      font: 13px/1.35 system-ui, sans-serif; overflow: hidden;
      transform: scale(var(--cpb-scale, 1)); transform-origin: top left;
      opacity: var(--cpb-opacity, .92);
    }
    #comfy-progress-bridge-panel[data-theme="light"] {
      --cpb-panel: #f1f3f8; --cpb-card: #fff; --cpb-text: #202430; --cpb-muted: #667085;
    }
    #comfy-progress-bridge-panel[data-theme="system"] {
      --cpb-panel: var(--comfy-menu-bg, #202020);
      --cpb-text: var(--fg-color, #f4f6fc);
      --cpb-muted: var(--descrip-text, #aab1c3);
    }
    #comfy-progress-bridge-panel .cpb-header {
      display: flex; align-items: center; gap: 7px; padding: 7px 8px;
      background: color-mix(in srgb, var(--cpb-card) 80%, transparent);
      border-bottom: 1px solid color-mix(in srgb, var(--cpb-text) 10%, transparent);
      font-weight: 650; user-select: none; cursor: grab; touch-action: none;
    }
    #comfy-progress-bridge-panel.cpb-dragging .cpb-header { cursor: grabbing; }
    #comfy-progress-bridge-panel .cpb-drag-handle {
      display: inline-grid; place-items: center; width: 20px; height: 24px;
      color: var(--cpb-muted); cursor: grab; font-size: 16px; line-height: 1;
      border-radius: 5px; outline: none;
    }
    #comfy-progress-bridge-panel .cpb-drag-handle:focus-visible {
      box-shadow: 0 0 0 2px var(--cpb-accent);
    }
    #comfy-progress-bridge-panel .cpb-dot {
      width: 8px; height: 8px; border-radius: 50%; background: #858585; flex: 0 0 auto;
    }
    #comfy-progress-bridge-panel[data-status="running"] .cpb-dot {
      background: var(--cpb-accent); box-shadow: 0 0 8px var(--cpb-accent);
    }
    #comfy-progress-bridge-panel[data-status="success"] .cpb-dot { background: #55c985; }
    #comfy-progress-bridge-panel[data-status="error"] .cpb-dot,
    #comfy-progress-bridge-panel[data-status="interrupted"] .cpb-dot { background: #ff6b6b; }
    #comfy-progress-bridge-panel .cpb-title { flex: 1; min-width: 0; }
    #comfy-progress-bridge-panel button,
    #comfy-progress-bridge-panel select,
    #comfy-progress-bridge-panel input { font: inherit; }
    #comfy-progress-bridge-panel .cpb-header button {
      width: 28px; height: 26px; padding: 0; border: 0; border-radius: 6px;
      color: inherit; background: color-mix(in srgb, var(--cpb-text) 10%, transparent);
      cursor: pointer; font-size: 14px;
    }
    #comfy-progress-bridge-panel .cpb-header button:hover,
    #comfy-progress-bridge-panel .cpb-header button[aria-expanded="true"] {
      background: color-mix(in srgb, var(--cpb-accent) 28%, transparent);
    }
    #comfy-progress-bridge-panel .cpb-body { padding: 9px 10px 11px; }
    #comfy-progress-bridge-panel.cpb-collapsed .cpb-body { display: none; }
    #comfy-progress-bridge-panel .cpb-rows {
      padding: 7px 8px; background: color-mix(in srgb, var(--cpb-card) 88%, transparent);
      border: 1px solid color-mix(in srgb, var(--cpb-accent) 45%, transparent);
      border-radius: 8px;
    }
    #comfy-progress-bridge-panel .cpb-row { display: flex; gap: 8px; margin: 3px 0; }
    #comfy-progress-bridge-panel .cpb-label { color: var(--cpb-muted); min-width: 54px; }
    #comfy-progress-bridge-panel .cpb-value {
      min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    #comfy-progress-bridge-panel .cpb-track {
      height: 7px; margin-top: 9px; border-radius: 99px; overflow: hidden;
      background: color-mix(in srgb, var(--cpb-text) 12%, transparent);
    }
    #comfy-progress-bridge-panel .cpb-fill {
      width: 0; height: 100%; background: var(--cpb-accent); transition: width 120ms linear;
    }
    #comfy-progress-bridge-panel .cpb-settings {
      padding: 9px 10px 10px; background: var(--cpb-panel);
      border-bottom: 1px solid color-mix(in srgb, var(--cpb-text) 10%, transparent);
    }
    #comfy-progress-bridge-panel .cpb-settings[hidden] { display: none; }
    #comfy-progress-bridge-panel .cpb-setting-row {
      display: grid; grid-template-columns: 72px 1fr 38px; align-items: center;
      gap: 8px; min-height: 30px;
    }
    #comfy-progress-bridge-panel .cpb-setting-row label { color: var(--cpb-muted); }
    #comfy-progress-bridge-panel .cpb-setting-row output { text-align: right; color: var(--cpb-muted); }
    #comfy-progress-bridge-panel .cpb-setting-row select {
      grid-column: 2 / 4; min-width: 0; color: var(--cpb-text); background: var(--cpb-card);
      border: 1px solid color-mix(in srgb, var(--cpb-text) 18%, transparent); border-radius: 5px;
      padding: 4px 6px;
    }
    #comfy-progress-bridge-panel .cpb-setting-row input[type="range"] {
      width: 100%; accent-color: var(--cpb-accent);
    }
    #comfy-progress-bridge-panel .cpb-reset-position {
      width: 100%; margin-top: 7px; padding: 6px 8px; color: var(--cpb-text);
      background: color-mix(in srgb, var(--cpb-accent) 18%, var(--cpb-card));
      border: 1px solid color-mix(in srgb, var(--cpb-accent) 45%, transparent);
      border-radius: 6px; cursor: pointer;
    }
  `;
  document.head.appendChild(style);
}

function row(label) {
  const element = document.createElement("div");
  element.className = "cpb-row";
  const name = document.createElement("span");
  name.className = "cpb-label";
  name.textContent = label;
  const value = document.createElement("span");
  value.className = "cpb-value";
  element.append(name, value);
  return { element, value };
}

function rangeSetting(labelText, minimum, maximum, value) {
  const rowElement = document.createElement("div");
  rowElement.className = "cpb-setting-row";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "range";
  input.min = String(minimum);
  input.max = String(maximum);
  input.value = String(value);
  const output = document.createElement("output");
  rowElement.append(label, input, output);
  return { rowElement, input, output };
}

function createSettings(preferences) {
  const settings = document.createElement("div");
  settings.className = "cpb-settings";
  settings.hidden = true;

  const themeRow = document.createElement("div");
  themeRow.className = "cpb-setting-row";
  const themeLabel = document.createElement("label");
  themeLabel.textContent = "Theme";
  const theme = document.createElement("select");
  theme.setAttribute("aria-label", "Progress panel theme");
  for (const [value, label] of [["system", "System"], ["dark", "Dark"], ["light", "Light"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    theme.appendChild(option);
  }
  theme.value = preferences.theme;
  themeRow.append(themeLabel, theme);

  const opacity = rangeSetting("Opacity", 55, 100, preferences.opacity);
  opacity.input.setAttribute("aria-label", "Progress panel opacity");
  const scale = rangeSetting("Scale", 80, 125, preferences.scale);
  scale.input.setAttribute("aria-label", "Progress panel scale");
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "cpb-reset-position";
  reset.textContent = "Reset position";
  settings.append(themeRow, opacity.rowElement, scale.rowElement, reset);
  return { settings, theme, opacity, scale, reset };
}

function createPanel() {
  addStyles();
  const storage = browserStorage();
  let preferences = readPreferences();

  const panel = document.createElement("section");
  panel.id = "comfy-progress-bridge-panel";
  panel.setAttribute("aria-label", "ComfyUI Progress");
  const header = document.createElement("div");
  header.className = "cpb-header";
  const dragHandle = document.createElement("span");
  dragHandle.className = "cpb-drag-handle";
  dragHandle.tabIndex = 0;
  dragHandle.setAttribute("role", "button");
  dragHandle.setAttribute("aria-label", "Move progress panel");
  dragHandle.textContent = "⠿";
  const dot = document.createElement("span");
  dot.className = "cpb-dot";
  const title = document.createElement("span");
  title.className = "cpb-title";
  title.textContent = "ComfyUI Progress";
  const settingsButton = document.createElement("button");
  settingsButton.type = "button";
  settingsButton.className = "cpb-settings-button";
  settingsButton.setAttribute("aria-label", "Progress panel settings");
  settingsButton.setAttribute("aria-expanded", "false");
  settingsButton.textContent = "⚙";
  const collapse = document.createElement("button");
  collapse.type = "button";
  collapse.className = "cpb-collapse";
  collapse.setAttribute("aria-label", "Collapse progress panel");
  collapse.textContent = "▾";
  header.append(dragHandle, dot, title, settingsButton, collapse);

  const controls = createSettings(preferences);
  const body = document.createElement("div");
  body.className = "cpb-body";
  const rows = document.createElement("div");
  rows.className = "cpb-rows";
  const track = document.createElement("div");
  track.className = "cpb-track";
  const fill = document.createElement("div");
  fill.className = "cpb-fill";
  track.appendChild(fill);
  body.append(rows, track);
  panel.append(header, controls.settings, body);

  const status = row("Status");
  const endpoint = row("Server");
  const node = row("Node");
  const queue = row("Queue");
  rows.append(status.element, endpoint.element, node.element, queue.element);
  endpoint.value.textContent = window.location.host || "ComfyUI";

  const save = () => {
    preferences = writePanelPreferences(storage, preferences);
    writeCollapsed(preferences.collapsed);
  };

  const applyAppearance = () => {
    panel.dataset.theme = preferences.theme;
    panel.style.setProperty("--cpb-opacity", String(preferences.opacity / 100));
    panel.style.setProperty("--cpb-scale", String(preferences.scale / 100));
    controls.theme.value = preferences.theme;
    controls.opacity.input.value = String(preferences.opacity);
    controls.opacity.output.textContent = `${preferences.opacity}%`;
    controls.scale.input.value = String(preferences.scale);
    controls.scale.output.textContent = `${preferences.scale}%`;
  };

  const applyPosition = (position) => {
    if (!position) {
      panel.style.left = "auto";
      panel.style.top = "58px";
      panel.style.right = "16px";
      preferences = { ...preferences, position: null };
      return;
    }
    const rect = panel.getBoundingClientRect();
    const safe = clampPanelPosition(position, rect, {
      width: window.innerWidth,
      height: window.innerHeight,
    });
    panel.style.left = `${safe.x}px`;
    panel.style.top = `${safe.y}px`;
    panel.style.right = "auto";
    preferences = { ...preferences, position: safe };
  };

  const setCollapsed = (collapsed) => {
    preferences = { ...preferences, collapsed };
    panel.classList.toggle("cpb-collapsed", collapsed);
    collapse.textContent = collapsed ? "▸" : "▾";
    collapse.setAttribute("aria-label", collapsed
      ? "Expand progress panel"
      : "Collapse progress panel");
    save();
  };

  applyAppearance();
  setCollapsed(preferences.collapsed);
  document.body.appendChild(panel);
  applyPosition(preferences.position);

  collapse.addEventListener("click", () => {
    setCollapsed(!panel.classList.contains("cpb-collapsed"));
    requestAnimationFrame(() => applyPosition(preferences.position));
  });
  settingsButton.addEventListener("click", () => {
    controls.settings.hidden = !controls.settings.hidden;
    settingsButton.setAttribute("aria-expanded", String(!controls.settings.hidden));
    requestAnimationFrame(() => applyPosition(preferences.position));
  });
  controls.theme.addEventListener("change", () => {
    preferences = { ...preferences, theme: controls.theme.value };
    applyAppearance();
    save();
  });
  controls.opacity.input.addEventListener("input", () => {
    preferences = { ...preferences, opacity: Number(controls.opacity.input.value) };
    applyAppearance();
    save();
  });
  controls.scale.input.addEventListener("input", () => {
    preferences = { ...preferences, scale: Number(controls.scale.input.value) };
    applyAppearance();
    applyPosition(preferences.position);
    save();
  });
  controls.reset.addEventListener("click", () => {
    applyPosition(null);
    save();
  });

  let drag = null;
  header.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button, select, input")) return;
    const rect = panel.getBoundingClientRect();
    drag = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    panel.classList.add("cpb-dragging");
    header.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  window.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    applyPosition({ x: event.clientX - drag.offsetX, y: event.clientY - drag.offsetY });
  });
  const finishDrag = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag = null;
    panel.classList.remove("cpb-dragging");
    header.releasePointerCapture?.(event.pointerId);
    save();
  };
  window.addEventListener("pointerup", finishDrag);
  window.addEventListener("pointercancel", finishDrag);

  dragHandle.addEventListener("keydown", (event) => {
    const movement = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    }[event.key];
    if (!movement) return;
    const rect = panel.getBoundingClientRect();
    const step = event.shiftKey ? 32 : 12;
    const origin = preferences.position || { x: rect.left, y: rect.top };
    applyPosition({ x: origin.x + movement[0] * step, y: origin.y + movement[1] * step });
    save();
    event.preventDefault();
  });

  window.addEventListener("resize", () => {
    if (!preferences.position) return;
    applyPosition(preferences.position);
    save();
  });

  return {
    panel,
    status: status.value,
    node: node.value,
    queue: queue.value,
    fill,
  };
}

function nodeName(nodeId) {
  if (!nodeId) return "—";
  const graphNode = app.graph?.getNodeById?.(nodeId);
  return graphNode?.title || graphNode?.type || `#${nodeId}`;
}

app.registerExtension({
  name: "Comfy.ProgressBridge.Panel",
  setup() {
    if (document.getElementById("comfy-progress-bridge-panel")) return;
    const ui = createPanel();
    let state = createState();
    let queueCount = 0;
    let sequence = 0;
    const browserInstance = window.location.origin || "comfyui-browser";

    const render = () => {
      const view = viewModel(state);
      const labels = {
        idle: "Idle",
        running: view.max > 0 ? `Running · ${view.percent}%` : "Running",
        success: "Complete",
        error: "Failed",
        interrupted: "Interrupted",
      };
      ui.panel.dataset.status = view.status;
      ui.status.textContent = labels[view.status] || view.status;
      ui.node.textContent = nodeName(view.nodeId);
      ui.queue.textContent = String(queueCount);
      ui.fill.style.width = `${view.percent}%`;
    };

    const dispatch = (type, detail = {}) => {
      const data = { ...detail };
      for (const key of ["prompt_id", "node", "node_id", "display_node"]) {
        if (data[key] !== undefined && data[key] !== null) data[key] = String(data[key]);
      }
      state = reduceEnvelope(state, {
        schema: 2,
        instance_id: browserInstance,
        sequence: ++sequence,
        type,
        data,
      });
      render();
    };

    api.addEventListener("executing", (event) => {
      const type = event.detail?.node === null ? "execution_success" : "executing";
      dispatch(type, event.detail);
    });
    api.addEventListener("progress", (event) => dispatch("progress", event.detail));
    api.addEventListener("execution_error", (event) => dispatch("execution_error", event.detail));
    api.addEventListener("execution_interrupted", (event) => dispatch("execution_interrupted", event.detail));
    api.addEventListener("execution_success", (event) => dispatch("execution_success", event.detail));
    api.addEventListener("status", (event) => {
      const status = event.detail?.exec_info || event.detail?.status?.exec_info || {};
      queueCount = Number.isFinite(status.queue_remaining) ? status.queue_remaining : queueCount;
      if (queueCount === 0 && viewModel(state).status === "running") dispatch("idle");
      render();
    });
    render();
  },
});
