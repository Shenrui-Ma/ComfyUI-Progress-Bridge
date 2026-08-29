"""Immutable identities and transport-neutral monitor state models."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID

MAX_PROMPT_ID_LENGTH = 1024
MAX_SEQUENCE = (1 << 63) - 1


def _bounded_string(value: object, name: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


def _finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    # Avoid math.isfinite converting adversarial, arbitrarily large ints to float.
    if isinstance(value, int) and value.bit_length() > 1024:
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True, order=True)
class EndpointId:
    """A single generation of a ComfyUI HTTP endpoint."""

    host: str
    port: int
    instance_id: UUID

    def __post_init__(self) -> None:
        _bounded_string(self.host, "endpoint host", maximum=255)
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("endpoint port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ValueError("endpoint port must be between 1 and 65535")
        if not isinstance(self.instance_id, UUID):
            raise ValueError("instance_id must be a UUID")


@dataclass(frozen=True, order=True)
class TaskKey:
    endpoint: EndpointId
    prompt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointId):
            raise ValueError("task endpoint must be an EndpointId")
        _bounded_string(self.prompt_id, "prompt_id", maximum=MAX_PROMPT_ID_LENGTH)


@dataclass(frozen=True)
class EventEnvelope:
    endpoint: EndpointId
    sequence: int
    observed_at: float
    type: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointId):
            raise ValueError("event endpoint must be an EndpointId")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("sequence must be an integer")
        if not 0 <= self.sequence <= MAX_SEQUENCE:
            raise ValueError(f"sequence must be between 0 and {MAX_SEQUENCE}")
        _finite_number(self.observed_at, "observed_at")
        _bounded_string(self.type, "event type", maximum=128)
        if not isinstance(self.data, Mapping):
            raise ValueError("event data must be a mapping")
        try:
            copied_data = dict(self.data)
        except Exception as exc:
            raise ValueError("event data must be a valid mapping") from exc
        object.__setattr__(self, "data", MappingProxyType(copied_data))


@dataclass(frozen=True)
class QueueSnapshot:
    endpoint: EndpointId
    online: bool
    running_prompt_ids: tuple[str, ...] = ()
    pending_prompt_ids: tuple[str, ...] = ()
    observed_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointId):
            raise ValueError("snapshot endpoint must be an EndpointId")
        if not isinstance(self.online, bool):
            raise ValueError("snapshot online must be a bool")
        for name, prompt_ids in (
            ("running_prompt_ids", self.running_prompt_ids),
            ("pending_prompt_ids", self.pending_prompt_ids),
        ):
            if not isinstance(prompt_ids, tuple):
                raise ValueError(f"{name} must be a tuple")
            for prompt_id in prompt_ids:
                _bounded_string(prompt_id, "prompt_id", maximum=MAX_PROMPT_ID_LENGTH)
        if self.observed_at is not None:
            _finite_number(self.observed_at, "observed_at")

    @classmethod
    def offline(cls, endpoint: EndpointId) -> QueueSnapshot:
        return cls(endpoint=endpoint, online=False)

    @property
    def busy(self) -> bool:
        return bool(self.running_prompt_ids or self.pending_prompt_ids)


@dataclass(frozen=True)
class TaskState:
    key: TaskKey
    status: str
    stage_key: str = "executing"
    node_name: str | None = None
    node_type: str | None = None
    progress_value: float | int | None = None
    progress_max: float | int | None = None
    error_message: str | None = None
    terminal_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"success", "error", "interrupted"}


@dataclass(frozen=True)
class EndpointState:
    endpoint: EndpointId
    online: bool | None = None
    busy: bool = False
    # None means no authoritative online queue snapshot has been observed yet.
    active_prompt_ids: frozenset[str] | None = None
    require_snapshot_for_unknown_nonterminal: bool = False
    # Monotonic within one endpoint process generation. The reducer owns epochs.
    busy_epoch: int = 0


@dataclass(frozen=True)
class MonitorState:
    endpoints: Mapping[EndpointId, EndpointState] = field(
        default_factory=lambda: MappingProxyType({})
    )
    tasks: Mapping[TaskKey, TaskState] = field(default_factory=lambda: MappingProxyType({}))
    last_sequences: Mapping[EndpointId, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    terminal_tombstones: Mapping[TaskKey, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_parts(
        cls,
        endpoints: Mapping[EndpointId, EndpointState],
        tasks: Mapping[TaskKey, TaskState],
        last_sequences: Mapping[EndpointId, int],
        terminal_tombstones: Mapping[TaskKey, int] | None = None,
    ) -> MonitorState:
        return cls(
            endpoints=MappingProxyType(dict(endpoints)),
            tasks=MappingProxyType(dict(tasks)),
            last_sequences=MappingProxyType(dict(last_sequences)),
            terminal_tombstones=MappingProxyType(dict(terminal_tombstones or {})),
        )


@dataclass(frozen=True)
class Transition:
    kind: str
    endpoint: EndpointId
    task: TaskKey | None = None
    observed_at: float | None = None
    busy_epoch: int | None = None


@dataclass(frozen=True)
class Reduction:
    state: MonitorState
    transitions: tuple[Transition, ...] = ()
    accepted: bool | None = None
