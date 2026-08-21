"""Pure reducers for endpoint-qualified queue snapshots and execution events."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .models import (
    MAX_PROMPT_ID_LENGTH,
    EndpointId,
    EndpointState,
    EventEnvelope,
    MonitorState,
    QueueSnapshot,
    Reduction,
    TaskKey,
    TaskState,
    Transition,
)
from .stages import stage_for_node

TERMINAL_EVENTS = {
    "execution_success": "success",
    "execution_error": "error",
    "execution_interrupted": "interrupted",
}
SUPPORTED_EVENTS = {"executing", "progress", *TERMINAL_EVENTS}
MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT = 256
MAX_TERMINAL_TOMBSTONES = 4096
MAX_EVENT_TEXT_LENGTH = 4096


def _parts(state: MonitorState):
    return (
        dict(state.endpoints),
        dict(state.tasks),
        dict(state.last_sequences),
        dict(state.terminal_tombstones),
    )


def _state(
    endpoints: dict[EndpointId, EndpointState],
    tasks: dict[TaskKey, TaskState],
    sequences: dict[EndpointId, int],
    tombstones: dict[TaskKey, int],
) -> MonitorState:
    return MonitorState.from_parts(
        endpoints, tasks, sequences, terminal_tombstones=tombstones
    )


def _require_state(state: object) -> MonitorState:
    if not isinstance(state, MonitorState):
        raise ValueError("state must be a MonitorState")
    return state


def _finite_number(value: object, name: str, *, nonnegative: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, int) and value.bit_length() > 1024:
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite or (nonnegative and value < 0):
        qualifier = "non-negative finite" if nonnegative else "finite"
        raise ValueError(f"{name} must be a {qualifier} number")
    return value


def _current_endpoint(
    endpoint: EndpointId, endpoints: dict[EndpointId, EndpointState]
) -> EndpointId | None:
    return next(
        (
            item
            for item in endpoints
            if item.host == endpoint.host and item.port == endpoint.port
        ),
        None,
    )


def _discard_generation(
    endpoint: EndpointId,
    endpoints: dict[EndpointId, EndpointState],
    tasks: dict[TaskKey, TaskState],
    sequences: dict[EndpointId, int],
    tombstones: dict[TaskKey, int],
) -> None:
    endpoints.pop(endpoint, None)
    sequences.pop(endpoint, None)
    for key in tuple(tasks):
        if key.endpoint == endpoint:
            tasks.pop(key)
    for key in tuple(tombstones):
        if key.endpoint == endpoint:
            tombstones.pop(key)


def _accept_event_generation(
    endpoint: EndpointId,
    endpoints: dict[EndpointId, EndpointState],
) -> bool:
    """Events may establish an empty address, but never arbitrate UUID changes."""
    current = _current_endpoint(endpoint, endpoints)
    return current is None or current == endpoint


def _accept_snapshot_generation(
    endpoint: EndpointId,
    endpoints: dict[EndpointId, EndpointState],
    tasks: dict[TaskKey, TaskState],
    sequences: dict[EndpointId, int],
    tombstones: dict[TaskKey, int],
) -> tuple[Transition, ...]:
    """An authoritative snapshot is the sole arbiter of generation replacement."""
    current = _current_endpoint(endpoint, endpoints)
    if current is None or current == endpoint:
        return ()
    _discard_generation(current, endpoints, tasks, sequences, tombstones)
    return (Transition("instance_replaced", endpoint),)


def _expire_parts(
    endpoints: dict[EndpointId, EndpointState],
    tasks: dict[TaskKey, TaskState],
    sequences: dict[EndpointId, int],
    tombstones: dict[TaskKey, int],
    now: float | int,
    retention: float | int,
) -> Reduction:
    transitions = []
    for key, task in tuple(tasks.items()):
        if task.terminal_at is not None and now >= task.terminal_at + retention:
            tasks.pop(key)
            transitions.append(Transition("task_expired", key.endpoint, key))
    return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))


def expire_terminal_tasks(
    state: MonitorState, *, now: float, terminal_retention: float = 30.0
) -> Reduction:
    """Expire terminal cards while preserving bounded absorption metadata."""
    _require_state(state)
    valid_now = _finite_number(now, "now")
    retention = _finite_number(terminal_retention, "terminal_retention", nonnegative=True)
    return _expire_parts(*_parts(state), valid_now, retention)


def reduce_snapshot(
    state: MonitorState,
    snapshot: QueueSnapshot,
    *,
    now: float,
    terminal_retention: float = 30.0,
) -> Reduction:
    """Apply one authoritative endpoint queue snapshot."""
    _require_state(state)
    if not isinstance(snapshot, QueueSnapshot):
        raise ValueError("snapshot must be a QueueSnapshot")
    expired = expire_terminal_tasks(state, now=now, terminal_retention=terminal_retention)
    endpoints, tasks, sequences, tombstones = _parts(expired.state)
    transitions = list(expired.transitions)
    current = _current_endpoint(snapshot.endpoint, endpoints)

    if not snapshot.online:
        # Loss of observation cannot arbitrate UUIDs, reconcile the queue, or
        # clear terminal tombstones. It may only establish/mark this generation
        # when no conflicting generation is already known.
        if current is not None and current != snapshot.endpoint:
            return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
        previous = endpoints.get(snapshot.endpoint, EndpointState(snapshot.endpoint))
        endpoints[snapshot.endpoint] = replace(previous, online=False)
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))

    transitions.extend(
        _accept_snapshot_generation(
            snapshot.endpoint, endpoints, tasks, sequences, tombstones
        )
    )
    previous = endpoints.get(snapshot.endpoint, EndpointState(snapshot.endpoint))

    busy = snapshot.busy
    if previous.busy and not busy:
        transitions.append(Transition("queue_completed", snapshot.endpoint))

    running = set(snapshot.running_prompt_ids)
    pending = set(snapshot.pending_prompt_ids)
    present = frozenset(running | pending)
    endpoints[snapshot.endpoint] = EndpointState(
        snapshot.endpoint,
        online=True,
        busy=busy,
        active_prompt_ids=present,
        require_snapshot_for_unknown_nonterminal=False,
    )

    # The snapshot reconciles every pre-snapshot terminal. An active prompt with
    # the same ID is a new epoch; an absent one remains protected by `present`.
    for key in tuple(tombstones):
        if key.endpoint == snapshot.endpoint:
            tombstones.pop(key)
    for key, task in tuple(tasks.items()):
        if key.endpoint != snapshot.endpoint:
            continue
        if task.terminal and key.prompt_id in present:
            tasks.pop(key)
        elif not task.terminal and key.prompt_id not in present:
            tasks.pop(key)

    for prompt_id in running:
        key = TaskKey(snapshot.endpoint, prompt_id)
        existing = tasks.get(key)
        tasks[key] = replace(existing, status="running") if existing else TaskState(key, "running")
    for prompt_id in pending:
        key = TaskKey(snapshot.endpoint, prompt_id)
        existing = tasks.get(key)
        tasks[key] = replace(existing, status="pending") if existing else TaskState(key, "pending")

    return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))


def _string(
    data: Mapping[str, Any], key: str, *, maximum: int = MAX_EVENT_TEXT_LENGTH
) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and len(value) <= maximum else None


def _number(
    data: Mapping[str, Any], key: str, fallback: float | int | None
) -> float | int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if isinstance(value, int) and value.bit_length() > 1024:
        return fallback
    try:
        return value if math.isfinite(value) else fallback
    except (OverflowError, TypeError, ValueError):
        return fallback


def _record_tombstone(
    endpoints: dict[EndpointId, EndpointState],
    tombstones: dict[TaskKey, int],
    key: TaskKey,
    sequence: int,
) -> None:
    """Record exact absorption while possible, then fail closed until polling."""
    if key in tombstones:
        tombstones[key] = sequence
        return
    own_count = sum(item.endpoint == key.endpoint for item in tombstones)
    if (
        own_count >= MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT
        or len(tombstones) >= MAX_TERMINAL_TOMBSTONES
    ):
        endpoints[key.endpoint] = replace(
            endpoints[key.endpoint], require_snapshot_for_unknown_nonterminal=True
        )
        return
    tombstones[key] = sequence
    if (
        own_count + 1 >= MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT
        or len(tombstones) >= MAX_TERMINAL_TOMBSTONES
    ):
        endpoints[key.endpoint] = replace(
            endpoints[key.endpoint], require_snapshot_for_unknown_nonterminal=True
        )


def reduce_event(
    state: MonitorState,
    event: EventEnvelope,
    *,
    now: float,
    terminal_retention: float = 30.0,
) -> Reduction:
    """Apply one ordered schema-v2 execution event."""
    _require_state(state)
    if not isinstance(event, EventEnvelope):
        raise ValueError("event must be an EventEnvelope")
    expired = expire_terminal_tasks(state, now=now, terminal_retention=terminal_retention)
    endpoints, tasks, sequences, tombstones = _parts(expired.state)
    transitions = list(expired.transitions)

    if not _accept_event_generation(event.endpoint, endpoints):
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
    if event.type not in SUPPORTED_EVENTS or event.sequence <= sequences.get(event.endpoint, -1):
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
    sequences[event.endpoint] = event.sequence
    endpoints.setdefault(event.endpoint, EndpointState(event.endpoint))

    prompt_id = _string(event.data, "prompt_id", maximum=MAX_PROMPT_ID_LENGTH)
    if not prompt_id:
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
    key = TaskKey(event.endpoint, prompt_id)
    if key in tombstones:
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))

    endpoint_state = endpoints[event.endpoint]
    existing = tasks.get(key)
    if existing is not None and existing.terminal:
        return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
    if event.type not in TERMINAL_EVENTS and existing is None:
        if endpoint_state.require_snapshot_for_unknown_nonterminal:
            return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))
        active = endpoint_state.active_prompt_ids
        if active is not None and prompt_id not in active:
            return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))

    existing = existing or TaskState(key, "running")
    node_name = (
        _string(event.data, "display_node")
        or _string(event.data, "node")
        or _string(event.data, "node_id")
        or existing.node_name
    )
    node_type = _string(event.data, "node_type") or existing.node_type
    stage = stage_for_node(node_type, node_name)

    if event.type in TERMINAL_EVENTS:
        status = TERMINAL_EVENTS[event.type]
        tasks[key] = replace(
            existing,
            status=status,
            stage_key=stage.key,
            node_name=stage.node_name,
            node_type=node_type,
            error_message=_string(event.data, "exception_message"),
            terminal_at=_finite_number(now, "now"),
        )
        _record_tombstone(endpoints, tombstones, key, event.sequence)
        transitions.append(Transition(f"task_{status}", event.endpoint, key))
    else:
        tasks[key] = replace(
            existing,
            status="running",
            stage_key=stage.key,
            node_name=stage.node_name,
            node_type=node_type,
            progress_value=_number(event.data, "value", existing.progress_value),
            progress_max=_number(event.data, "max", existing.progress_max),
            error_message=None,
            terminal_at=None,
        )

    return Reduction(_state(endpoints, tasks, sequences, tombstones), tuple(transitions))


class MonitorReducer:
    """Small stateful facade over the pure reducers with an injected clock."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        terminal_retention: float = 30.0,
        state: MonitorState | None = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.terminal_retention = _finite_number(
            terminal_retention, "terminal_retention", nonnegative=True
        )
        if state is not None and not isinstance(state, MonitorState):
            raise ValueError("state must be a MonitorState")
        self.clock = clock
        self.state = state if state is not None else MonitorState()

    def apply_snapshot(self, snapshot: QueueSnapshot) -> Reduction:
        result = reduce_snapshot(
            self.state,
            snapshot,
            now=self.clock(),
            terminal_retention=self.terminal_retention,
        )
        self.state = result.state
        return result

    def apply_event(self, event: EventEnvelope) -> Reduction:
        result = reduce_event(
            self.state,
            event,
            now=self.clock(),
            terminal_retention=self.terminal_retention,
        )
        self.state = result.state
        return result

    def expire(self) -> Reduction:
        result = expire_terminal_tasks(
            self.state,
            now=self.clock(),
            terminal_retention=self.terminal_retention,
        )
        self.state = result.state
        return result
