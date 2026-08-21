import math
from collections.abc import Mapping
from uuid import UUID

import pytest

from comfyui_progress_bridge.monitor.models import (
    EndpointId,
    EventEnvelope,
    MonitorState,
    QueueSnapshot,
    TaskKey,
)
from comfyui_progress_bridge.monitor.reducer import (
    MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT,
    MonitorReducer,
    reduce_event,
    reduce_snapshot,
)
from comfyui_progress_bridge.monitor.stages import stage_for_node


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def endpoint(port, instance):
    return EndpointId("127.0.0.1", port, UUID(instance))


ONE = endpoint(8189, "00000000-0000-0000-0000-000000000001")
TWO = endpoint(9189, "00000000-0000-0000-0000-000000000002")


def event(ep, sequence, event_type="progress", prompt_id="same", **data):
    return EventEnvelope(
        endpoint=ep,
        sequence=sequence,
        observed_at=10.0,
        type=event_type,
        data={"prompt_id": prompt_id, **data},
    )


def test_identical_prompt_ids_are_endpoint_qualified():
    state = MonitorState()
    state = reduce_event(state, event(ONE, 1, node="1", value=1, max=2), now=100).state
    state = reduce_event(state, event(TWO, 1, node="2", value=2, max=3), now=100).state

    assert set(state.tasks) == {TaskKey(ONE, "same"), TaskKey(TWO, "same")}
    assert state.tasks[TaskKey(ONE, "same")].node_name == "1"
    assert state.tasks[TaskKey(TWO, "same")].node_name == "2"


def test_only_authoritative_online_busy_to_empty_emits_once_per_busy_epoch():
    reducer = MonitorReducer(clock=Clock())
    assert reducer.apply_snapshot(QueueSnapshot(ONE, True, ("a",), ())).transitions == ()
    drained = reducer.apply_snapshot(QueueSnapshot(ONE, True, (), ()))
    assert [item.kind for item in drained.transitions] == ["queue_completed"]
    assert drained.transitions[0].busy_epoch == 1
    assert reducer.apply_snapshot(QueueSnapshot(ONE, True, (), ())).transitions == ()

    # A different endpoint and a new busy epoch are independent.
    reducer.apply_snapshot(QueueSnapshot(TWO, True, ("b",), ()))
    reducer.apply_snapshot(QueueSnapshot(ONE, True, (), ("c",)))
    second = reducer.apply_snapshot(QueueSnapshot(ONE, True, (), ()))
    assert [item.kind for item in second.transitions] == ["queue_completed"]
    assert second.transitions[0].busy_epoch == 2
    assert reducer.state.endpoints[TWO].busy is True


def test_offline_or_missing_is_not_an_empty_queue():
    state = reduce_snapshot(
        MonitorState(), QueueSnapshot(ONE, True, ("a",), ()), now=100
    ).state
    offline = reduce_snapshot(state, QueueSnapshot.offline(ONE), now=101)
    assert offline.transitions == ()
    assert offline.state.endpoints[ONE].online is False
    assert offline.state.endpoints[ONE].busy is True


def test_duplicate_and_stale_event_sequences_are_ignored():
    state = reduce_event(MonitorState(), event(ONE, 4, value=4, max=10), now=100).state
    duplicate = reduce_event(state, event(ONE, 4, value=9, max=10), now=101)
    stale = reduce_event(duplicate.state, event(ONE, 3, value=8, max=10), now=102)
    assert duplicate.transitions == ()
    assert stale.state.tasks[TaskKey(ONE, "same")].progress_value == 4


def test_event_from_different_uuid_cannot_replace_known_generation():
    replacement = endpoint(8189, "00000000-0000-0000-0000-000000000009")
    state = reduce_event(MonitorState(), event(ONE, 20, value=1, max=2), now=100).state
    result = reduce_event(state, event(replacement, 1, value=2, max=2), now=101)

    assert set(result.state.endpoints) == {ONE}
    assert result.state.tasks[TaskKey(ONE, "same")].progress_value == 1
    assert replacement not in result.state.last_sequences
    assert result.transitions == ()


def test_replaced_instance_event_cannot_replace_authoritative_generation():
    replacement = endpoint(8189, "00000000-0000-0000-0000-000000000009")
    state = reduce_event(MonitorState(), event(ONE, 20, value=1), now=100).state
    state = reduce_snapshot(
        state, QueueSnapshot(replacement, True, ("same",), ()), now=101
    ).state
    late = reduce_event(state, event(ONE, 21, value=99), now=102)

    assert set(late.state.endpoints) == {replacement}
    assert TaskKey(replacement, "same") in late.state.tasks
    assert TaskKey(ONE, "same") not in late.state.tasks
    assert late.transitions == ()


def test_authoritative_snapshot_can_replace_an_already_known_generation():
    replacement = endpoint(8189, "00000000-0000-0000-0000-000000000009")
    state = reduce_snapshot(
        MonitorState(), QueueSnapshot(ONE, True, ("old",), ()), now=100
    ).state
    state = reduce_snapshot(
        state, QueueSnapshot(replacement, True, ("new",), ()), now=101
    ).state
    result = reduce_snapshot(state, QueueSnapshot(ONE, True, ("old",), ()), now=102)

    assert set(result.state.endpoints) == {ONE}
    assert TaskKey(ONE, "old") in result.state.tasks
    assert TaskKey(replacement, "new") not in result.state.tasks
    assert [item.kind for item in result.transitions] == ["instance_replaced"]


def test_terminal_state_retained_then_expires_with_injected_clock():
    clock = Clock()
    reducer = MonitorReducer(clock=clock, terminal_retention=30)
    reducer.apply_event(event(ONE, 1, "execution_success"))
    key = TaskKey(ONE, "same")
    assert reducer.state.tasks[key].status == "success"

    clock.value = 129.999
    reducer.expire()
    assert key in reducer.state.tasks
    clock.value = 130.0
    result = reducer.expire()
    assert key not in reducer.state.tasks
    assert [item.kind for item in result.transitions] == ["task_expired"]


def test_error_and_interrupted_are_terminal_and_duplicate_packet_does_not_extend_retention():
    clock = Clock()
    reducer = MonitorReducer(clock=clock)
    reducer.apply_event(event(ONE, 1, "execution_error", exception_message="bad"))
    clock.value = 110
    reducer.apply_event(event(ONE, 1, "execution_error", exception_message="bad"))
    clock.value = 130
    reducer.expire()
    assert TaskKey(ONE, "same") not in reducer.state.tasks

    reducer.apply_event(event(TWO, 1, "execution_interrupted"))
    assert reducer.state.tasks[TaskKey(TWO, "same")].status == "interrupted"


@pytest.mark.parametrize(
    "terminal_type", ["execution_success", "execution_error", "execution_interrupted"]
)
@pytest.mark.parametrize("later_type", ["executing", "progress"])
def test_terminal_task_is_absorbing_for_later_nonterminal_events(terminal_type, later_type):
    state = reduce_event(MonitorState(), event(ONE, 1, terminal_type), now=100).state
    result = reduce_event(state, event(ONE, 2, later_type, value=9, max=10), now=101)
    task = result.state.tasks[TaskKey(ONE, "same")]
    expected = {
        "execution_success": "success",
        "execution_error": "error",
        "execution_interrupted": "interrupted",
    }
    assert task.status == expected[terminal_type]
    assert task.terminal_at == 100


@pytest.mark.parametrize(
    "value", [True, False, math.nan, math.inf, -math.inf, [1], {"n": 1}, object()]
)
def test_reducer_does_not_retain_invalid_progress_numbers(value):
    state = reduce_event(MonitorState(), event(ONE, 1, value=3, max=10), now=100).state
    result = reduce_event(state, event(ONE, 2, value=value, max=value), now=101)
    task = result.state.tasks[TaskKey(ONE, "same")]
    assert task.progress_value == 3
    assert task.progress_max == 10


@pytest.mark.parametrize(
    "build",
    [
        lambda: EndpointId("127.0.0.1", 0, UUID(int=1)),
        lambda: EndpointId("127.0.0.1", True, UUID(int=1)),
        lambda: EndpointId("127.0.0.1", 8188, "not-a-uuid"),
        lambda: EventEnvelope(ONE, -1, 1.0, "progress", {}),
        lambda: EventEnvelope(ONE, True, 1.0, "progress", {}),
        lambda: EventEnvelope(ONE, 1, math.inf, "progress", {}),
        lambda: EventEnvelope(ONE, 1, True, "progress", {}),
    ],
)
def test_models_reject_invalid_identity_ordering_and_timestamp_fields(build):
    with pytest.raises((TypeError, ValueError)):
        build()


def test_stage_output_has_semantic_key_and_raw_node_name_separately():
    stage = stage_for_node(node_type="KSampler", node_name="最终采样器")
    assert stage.key == "sampling"
    assert stage.node_name == "最终采样器"


def test_terminal_absorption_survives_visual_retention_expiry():
    clock = Clock()
    reducer = MonitorReducer(clock=clock, terminal_retention=1)
    reducer.apply_event(event(ONE, 1, "execution_success"))
    clock.value = 102
    reducer.expire()

    result = reducer.apply_event(event(ONE, 2, "progress", value=99, max=100))

    key = TaskKey(ONE, "same")
    assert key not in result.state.tasks
    assert key in result.state.terminal_tombstones


def test_authoritative_busy_snapshot_clears_tombstone_for_prompt_reuse():
    reducer = MonitorReducer(clock=Clock(), terminal_retention=0)
    reducer.apply_event(event(ONE, 1, "execution_success", prompt_id="reused"))
    reducer.expire()

    # Only a later authoritative snapshot can establish a reused prompt epoch.
    reducer.apply_snapshot(QueueSnapshot(ONE, True, ("reused",), ()))
    task = reducer.state.tasks[TaskKey(ONE, "reused")]

    assert task.status == "running"
    assert TaskKey(ONE, "reused") not in reducer.state.terminal_tombstones


def test_generation_replacement_clears_old_tombstones():
    replacement = endpoint(8189, "00000000-0000-0000-0000-000000000009")
    state = reduce_event(MonitorState(), event(ONE, 1, "execution_success"), now=100).state

    state = reduce_snapshot(
        state, QueueSnapshot(replacement, True, ("same",), ()), now=101
    ).state

    assert all(key.endpoint != ONE for key in state.terminal_tombstones)


def test_terminal_tombstones_are_bounded_per_endpoint():
    state = MonitorState()
    for sequence in range(MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT + 3):
        state = reduce_event(
            state,
            event(ONE, sequence, "execution_success", prompt_id=f"prompt-{sequence}"),
            now=100,
        ).state

    endpoint_tombstones = [key for key in state.terminal_tombstones if key.endpoint == ONE]
    assert len(endpoint_tombstones) == MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT
    assert state.endpoints[ONE].require_snapshot_for_unknown_nonterminal is True


def test_257_terminals_cannot_resurrect_from_late_progress_before_snapshot():
    state = MonitorState()
    count = MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT + 1
    for sequence in range(count):
        state = reduce_event(
            state,
            event(ONE, sequence, "execution_success", prompt_id=f"prompt-{sequence}"),
            now=100,
            terminal_retention=0,
        ).state

    for sequence in range(count, count * 2):
        prompt = f"prompt-{sequence - count}"
        state = reduce_event(
            state,
            event(ONE, sequence, "progress", prompt_id=prompt, value=99),
            now=101,
            terminal_retention=0,
        ).state

    assert state.tasks == {}
    assert len(state.terminal_tombstones) == MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT
    assert state.endpoints[ONE].require_snapshot_for_unknown_nonterminal is True


def test_authoritative_snapshot_reconciles_strict_mode_and_active_prompt_ids():
    state = MonitorState()
    for sequence in range(MAX_TERMINAL_TOMBSTONES_PER_ENDPOINT + 1):
        state = reduce_event(
            state,
            event(ONE, sequence, "execution_success", prompt_id=f"done-{sequence}"),
            now=100,
            terminal_retention=0,
        ).state

    state = reduce_snapshot(
        state, QueueSnapshot(ONE, True, ("reused",), ()), now=101, terminal_retention=0
    ).state
    accepted = reduce_event(
        state, event(ONE, 1000, prompt_id="reused", value=3), now=102
    ).state
    ignored = reduce_event(
        accepted, event(ONE, 1001, prompt_id="unknown", value=4), now=103
    ).state

    assert accepted.endpoints[ONE].require_snapshot_for_unknown_nonterminal is False
    assert accepted.endpoints[ONE].active_prompt_ids == frozenset({"reused"})
    assert accepted.terminal_tombstones == {}
    assert TaskKey(ONE, "reused") in ignored.tasks
    assert TaskKey(ONE, "unknown") not in ignored.tasks


def test_repeated_authoritative_uuid_changes_keep_generation_state_bounded():
    state = MonitorState()
    for value in range(1, 301):
        generation = endpoint(8189, str(UUID(int=value)))
        state = reduce_snapshot(
            state, QueueSnapshot(generation, True, (f"prompt-{value}",), ()), now=value
        ).state

    assert len(state.endpoints) == 1
    assert len(state.tasks) == 1
    assert len(state.last_sequences) == 0
    assert len(state.terminal_tombstones) == 0


class BadMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError("broken mapping")

    def __iter__(self):
        raise RuntimeError("broken mapping")

    def __len__(self):
        raise RuntimeError("broken mapping")


def test_event_envelope_normalizes_arbitrary_bad_mapping_failures():
    with pytest.raises(ValueError, match="event data must be a valid mapping"):
        EventEnvelope(ONE, 1, 1.0, "progress", BadMapping())


@pytest.mark.parametrize(
    "build",
    [
        lambda: EventEnvelope(ONE, 1, 1.0, "progress", []),
        lambda: EventEnvelope(ONE, 1, 1.0, "progress", None),
        lambda: EventEnvelope(ONE, 10**1000, 1.0, "progress", {}),
        lambda: EventEnvelope(ONE, 1, 10**1000, "progress", {}),
        lambda: QueueSnapshot(ONE, 1),
        lambda: QueueSnapshot(ONE, True, ["prompt"], ()),
        lambda: QueueSnapshot(ONE, True, ([],), ()),
        lambda: QueueSnapshot(ONE, True, ("x" * 1025,), ()),
        lambda: TaskKey(ONE, []),
        lambda: TaskKey(ONE, "x" * 1025),
    ],
)
def test_transport_models_reject_malformed_values_with_clear_value_error(build):
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize("value", [10**1000, -(10**1000), math.nan, math.inf])
def test_oversized_and_nonfinite_progress_values_are_ignored(value):
    state = reduce_event(MonitorState(), event(ONE, 1, value=3, max=10), now=100).state
    result = reduce_event(state, event(ONE, 2, value=value, max=value), now=101)
    task = result.state.tasks[TaskKey(ONE, "same")]
    assert (task.progress_value, task.progress_max) == (3, 10)


@pytest.mark.parametrize("prompt_id", [["bad"], {"bad": 1}, 42, "x" * 1025])
def test_malformed_event_prompt_ids_are_safely_ignored(prompt_id):
    result = reduce_event(MonitorState(), event(ONE, 1, prompt_id=prompt_id), now=100)
    assert result.state.tasks == {}


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda value: reduce_event(MonitorState(), value, now=1), {}),
        (lambda value: reduce_snapshot(MonitorState(), value, now=1), []),
        (lambda value: reduce_event(value, event(ONE, 1), now=1), None),
        (lambda value: reduce_snapshot(value, QueueSnapshot(ONE, True), now=1), object()),
    ],
)
def test_public_reducers_reject_wrong_model_types_without_incidental_exceptions(call, argument):
    with pytest.raises(ValueError):
        call(argument)


@pytest.mark.parametrize("value", [None, "30", math.nan, math.inf, 10**1000])
def test_reducer_constructor_rejects_invalid_retention_with_value_error(value):
    with pytest.raises(ValueError):
        MonitorReducer(terminal_retention=value)
