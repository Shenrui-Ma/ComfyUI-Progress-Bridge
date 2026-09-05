import { createState, reduceEvent, viewModel } from "./progress-state.mjs";

export function createClientState() {
  return { connection: "connecting", queue: null, task: null, updatedAt: null };
}

// Extension setup can run after the WebSocket's initial status. GET /prompt returns
// queue counts only (unlike /queue, which contains workflows). Never overwrite newer events.
export async function initializeQueueStatus(api, dispatch, isCurrent) {
  if (typeof api.fetchApi !== "function") return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await api.fetchApi("/prompt", {signal: controller.signal});
    if (!response.ok) throw new Error("status unavailable");
    const data = await response.json();
    if (isCurrent()) dispatch("status", data);
  } catch {
    if (isCurrent()) dispatch("reconnecting");
  } finally {
    clearTimeout(timer);
  }
}

function identifier(value) {
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  return typeof value === "string" && value.length > 0 && value.length <= 256 ? value : null;
}

// Consume only the existing client WebSocket; never request another user's workflow.
export function reduceClientEvent(state, type, detail, now = Date.now()) {
  if (type === "reconnecting" || (type === "status" && detail === null)) {
    return { ...state, connection: "offline", queue: null, task: null };
  }
  if (type === "reconnected") return { ...createClientState(), updatedAt: state.updatedAt };
  // ComfyUI's browser API converts the wire executing payload to node ID/null.
  // Its execution_start event supplies the prompt identity; never invent one.
  if (type === "executing" && (detail === null || identifier(detail) !== null)) {
    if (state.task?.status !== "running") return state;
    detail = { prompt_id: state.task.promptId, node: detail };
  }
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return state;
  if (type === "status") {
    const remaining = (detail.exec_info ?? detail.status?.exec_info)?.queue_remaining;
    if (!Number.isSafeInteger(remaining) || remaining < 0) return state;
    const newEpoch = remaining > 0 && state.queue === 0;
    const task = newEpoch || (remaining === 0 && state.task?.status === "running")
      ? null : state.task;
    return { connection: "online", queue: remaining, task, updatedAt: now };
  }
  const supported = ["executing", "progress", "execution_start", "execution_success",
    "execution_error", "execution_interrupted"];
  if (!supported.includes(type)) return state;
  let promptId = identifier(detail.prompt_id);
  // Older progress messages omit prompt_id: correlate only to a known running task.
  if (!promptId && detail.prompt_id == null && type === "progress"
      && state.task?.status === "running") promptId = state.task.promptId;
  if (!promptId) return state;
  const terminal = type.startsWith("execution_") && type !== "execution_start";
  if (terminal && state.task && state.task.promptId !== promptId) return state;
  if (state.task?.promptId === promptId && state.task.status !== "running") return state;
  if (type === "progress" && (!Number.isFinite(detail.value) || detail.value < 0
      || !Number.isFinite(detail.max) || detail.max <= 0)) return state;
  // ComfyUI emits executing(node=null) after errors too. Preserve explicit outcomes.
  if (type === "executing" && detail.node === null) {
    if (!state.task || state.task.promptId !== promptId) return state;
    return { ...state, task: null, updatedAt: now };
  }
  const data = { prompt_id: promptId };
  const node = identifier(detail.display_node ?? detail.node_id ?? detail.node);
  if (node !== null) data.node = node;
  if (type === "progress") Object.assign(data, { value: detail.value, max: detail.max });
  const base = { ...createState(), task: state.task };
  const next = reduceEvent(base, { type: type === "execution_start" ? "executing" : type, data });
  if (next === base) return state;
  return { ...state, connection: "online", task: viewModel(next), updatedAt: now };
}
