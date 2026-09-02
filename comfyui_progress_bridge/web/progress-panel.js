import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createState, reduceEnvelope, viewModel } from "./progress-state.mjs";

const STORAGE_KEY = "comfy-progress-bridge-collapsed";

function readCollapsed() {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed) {
  try {
    localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
  } catch {
    // Restricted storage must not prevent the panel from working.
  }
}

function addStyles() {
  if (document.getElementById("comfy-progress-bridge-styles")) return;
  const style = document.createElement("style");
  style.id = "comfy-progress-bridge-styles";
  style.textContent = `
    #comfy-progress-bridge-panel {
      position: fixed; top: 58px; right: 16px; z-index: 10000; width: 300px;
      box-sizing: border-box; color: var(--fg-color, #eee);
      background: #202020eb;
      background: color-mix(in srgb, var(--comfy-menu-bg, #202020) 92%, transparent);
      border: 1px solid var(--border-color, #555); border-radius: 10px;
      box-shadow: 0 8px 28px #0008; backdrop-filter: blur(8px);
      font: 13px/1.35 system-ui, sans-serif; overflow: hidden;
    }
    #comfy-progress-bridge-panel .cpb-header {
      display: flex; align-items: center; gap: 8px; padding: 9px 10px;
      font-weight: 650; user-select: none;
    }
    #comfy-progress-bridge-panel .cpb-dot {
      width: 8px; height: 8px; border-radius: 50%; background: #858585;
    }
    #comfy-progress-bridge-panel[data-status="running"] .cpb-dot { background: #6c8eff; box-shadow: 0 0 8px #6c8eff; }
    #comfy-progress-bridge-panel[data-status="success"] .cpb-dot { background: #55c985; }
    #comfy-progress-bridge-panel[data-status="error"] .cpb-dot,
    #comfy-progress-bridge-panel[data-status="interrupted"] .cpb-dot { background: #ff6b6b; }
    #comfy-progress-bridge-panel .cpb-title { flex: 1; }
    #comfy-progress-bridge-panel button {
      width: 28px; height: 26px; padding: 0; border: 0; border-radius: 6px;
      color: inherit; background: color-mix(in srgb, var(--fg-color, #eee) 10%, transparent);
      cursor: pointer; font-size: 15px;
    }
    #comfy-progress-bridge-panel .cpb-body { padding: 0 10px 11px; }
    #comfy-progress-bridge-panel.cpb-collapsed .cpb-body { display: none; }
    #comfy-progress-bridge-panel .cpb-row { display: flex; gap: 8px; margin: 3px 0; }
    #comfy-progress-bridge-panel .cpb-label { color: var(--descrip-text, #aaa); min-width: 54px; }
    #comfy-progress-bridge-panel .cpb-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #comfy-progress-bridge-panel .cpb-track { height: 7px; margin-top: 9px; border-radius: 99px; overflow: hidden; background: #ffffff1a; }
    #comfy-progress-bridge-panel .cpb-fill { width: 0; height: 100%; background: #6c8eff; transition: width 120ms linear; }
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

function createPanel() {
  addStyles();
  const panel = document.createElement("section");
  panel.id = "comfy-progress-bridge-panel";
  panel.setAttribute("aria-label", "ComfyUI Progress");
  const header = document.createElement("div");
  header.className = "cpb-header";
  const dot = document.createElement("span");
  dot.className = "cpb-dot";
  const title = document.createElement("span");
  title.className = "cpb-title";
  title.textContent = "ComfyUI Progress";
  const collapse = document.createElement("button");
  collapse.type = "button";
  collapse.className = "cpb-collapse";
  collapse.setAttribute("aria-label", "Collapse progress panel");
  collapse.textContent = "▾";
  header.append(dot, title, collapse);

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
  panel.append(header, body);

  const status = row("Status");
  const endpoint = row("Server");
  const node = row("Node");
  const queue = row("Queue");
  rows.append(status.element, endpoint.element, node.element, queue.element);
  endpoint.value.textContent = window.location.host || "ComfyUI";

  const setCollapsed = (collapsed) => {
    panel.classList.toggle("cpb-collapsed", collapsed);
    collapse.textContent = collapsed ? "▸" : "▾";
    collapse.setAttribute("aria-label", collapsed ? "Expand progress panel" : "Collapse progress panel");
    writeCollapsed(collapsed);
  };
  setCollapsed(readCollapsed());
  collapse.addEventListener("click", () => setCollapsed(!panel.classList.contains("cpb-collapsed")));
  document.body.appendChild(panel);

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