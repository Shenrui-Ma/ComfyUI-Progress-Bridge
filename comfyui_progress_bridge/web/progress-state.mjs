const TERMINAL_TYPES = {
  execution_success: "success",
  execution_error: "error",
  execution_interrupted: "interrupted",
};

export function createState() {
  return {
    task: null,
    instanceId: null,
    latestSequence: -1,
  };
}

function validData(data) {
  return data && typeof data === "object" && typeof data.prompt_id === "string" && data.prompt_id;
}

export function reduceEvent(state, event) {
  if (!event || typeof event !== "object") return state;
  if (event.type === "idle") return { ...state, task: null };
  if (!validData(event.data)) return state;

  const data = event.data;
  const promptId = data.prompt_id;
  const previous = state.task?.promptId === promptId ? state.task : {
    status: "running",
    promptId,
    nodeId: null,
    value: 0,
    max: 0,
  };
  const task = { ...previous };

  if (event.type === "progress") {
    task.status = "running";
    if (typeof data.node === "string") task.nodeId = data.node;
    if (Number.isFinite(data.value)) task.value = data.value;
    if (Number.isFinite(data.max) && data.max >= 0) task.max = data.max;
  } else if (event.type === "executing") {
    task.status = "running";
    const node = data.display_node ?? data.node_id ?? data.node;
    if (typeof node === "string") task.nodeId = node;
  } else if (TERMINAL_TYPES[event.type]) {
    task.status = TERMINAL_TYPES[event.type];
    if (task.status === "success") {
      task.value = 1;
      task.max = 1;
    }
  } else {
    return state;
  }

  return { ...state, task };
}

export function reduceEnvelope(state, envelope) {
  if (
    !envelope ||
    envelope.schema !== 2 ||
    typeof envelope.instance_id !== "string" ||
    !Number.isInteger(envelope.sequence) ||
    envelope.sequence < 0
  ) {
    return state;
  }

  const sameInstance = state.instanceId === envelope.instance_id;
  if (sameInstance && envelope.sequence <= state.latestSequence) return state;
  const base = sameInstance ? state : createState();
  const reduced = reduceEvent(base, envelope);
  if (reduced === base) return state;
  return {
    ...reduced,
    instanceId: envelope.instance_id,
    latestSequence: envelope.sequence,
  };
}

export function viewModel(state) {
  const task = state.task;
  if (!task) {
    return {
      status: "idle",
      promptId: null,
      nodeId: null,
      value: 0,
      max: 0,
      percent: 0,
    };
  }
  const percent = task.max > 0
    ? Math.max(0, Math.min(100, Math.round((task.value / task.max) * 100)))
    : 0;
  return { ...task, percent };
}