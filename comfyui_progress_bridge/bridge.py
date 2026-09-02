"""Core implementation for the ComfyUI external progress event bridge."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import threading
import time
import uuid
import warnings
from collections.abc import Callable, Mapping
from typing import Any, TypeGuard

SUPPORTED_EVENTS = frozenset(
    {
        "executing",
        "progress",
        "execution_error",
        "execution_interrupted",
        "execution_success",
    }
)
FORWARDED_DATA_KEYS = frozenset(
    {
        "prompt_id",
        "node",
        "node_id",
        "display_node",
        "value",
        "max",
        "exception_message",
        "node_type",
    }
)

DEFAULT_BRIDGE_PORT = 30999

PROCESS_INSTANCE_ID = uuid.uuid4()
MAX_DATAGRAM_BYTES = 8192
MAX_PROMPT_ID_CHARS = 256
MAX_FIELD_CHARS = 1024
MAX_ERROR_CHARS = 4096


def _port(value: Any, variable: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{variable} must be an integer between 1 and 65535")
    return value


def _finite_number(value: Any) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def bridge_port(comfy_port: int, base_port: int | None = None) -> int:
    """Return the shared listener port (the arguments remain for API compatibility)."""
    _port(comfy_port, "comfy_port")
    if base_port is not None:
        warnings.warn(
            "base_port is ignored and will be removed in a future release",
            DeprecationWarning,
            stacklevel=2,
        )
    return DEFAULT_BRIDGE_PORT


def _numeric_ipv4(value: str, variable: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"{variable} must be a numeric IPv4 address") from exc


def resolve_target(
    comfy_port: int, environ: Mapping[str, str] | None = None
) -> tuple[str, int]:
    """Resolve the shared UDP destination without doing DNS resolution."""
    default_port = bridge_port(comfy_port)
    values = os.environ if environ is None else environ
    host = _numeric_ipv4(
        values.get("COMFY_PROGRESS_BRIDGE_HOST", "127.0.0.1"),
        "COMFY_PROGRESS_BRIDGE_HOST",
    )
    raw_port = values.get("COMFY_PROGRESS_BRIDGE_PORT")
    try:
        port = default_port if raw_port is None else int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("COMFY_PROGRESS_BRIDGE_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("COMFY_PROGRESS_BRIDGE_PORT must be between 1 and 65535")
    return host, port


def resolve_endpoint_host(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the numeric host advertised as the Comfy HTTP endpoint."""
    values = os.environ if environ is None else environ
    return _numeric_ipv4(
        values.get("COMFY_PROGRESS_BRIDGE_ENDPOINT_HOST", "127.0.0.1"),
        "COMFY_PROGRESS_BRIDGE_ENDPOINT_HOST",
    )


def compact_event(
    event: object,
    data: Any,
    *,
    endpoint_port: int = 8188,
    endpoint_host: str = "127.0.0.1",
    instance_id: str | uuid.UUID | None = None,
    sequence: int = 0,
    observed_at: float | None = None,
) -> bytes | None:
    """Serialize a bounded schema-v2 subset needed by an external monitor."""
    if not isinstance(event, str) or event not in SUPPORTED_EVENTS or not isinstance(data, dict):
        return None
    try:
        prompt_id = data.get("prompt_id")
        if (
            not isinstance(prompt_id, str)
            or not prompt_id
            or len(prompt_id) > MAX_PROMPT_ID_CHARS
        ):
            return None

        compact: dict[str, str | int | float] = {"prompt_id": prompt_id}
        string_keys = FORWARDED_DATA_KEYS - {"prompt_id", "value", "max"}
        for key in string_keys:
            value = data.get(key)
            if isinstance(value, str):
                limit = MAX_ERROR_CHARS if key == "exception_message" else MAX_FIELD_CHARS
                compact[key] = value[:limit]
        for key in ("value", "max"):
            value = data.get(key)
            if _finite_number(value):
                compact[key] = value

        port = _port(endpoint_port, "endpoint_port")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return None
        generation = PROCESS_INSTANCE_ID if instance_id is None else uuid.UUID(str(instance_id))
        timestamp = time.time() if observed_at is None else observed_at
        if not _finite_number(timestamp):
            return None

        envelope = {
            "schema": 2,
            "endpoint": {
                "host": _numeric_ipv4(endpoint_host, "endpoint host"),
                "port": port,
            },
            "instance_id": str(generation),
            "sequence": sequence,
            "observed_at": timestamp,
            "type": event,
            "data": compact,
        }
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return payload if len(payload) <= MAX_DATAGRAM_BYTES else None
    except Exception:
        return None


def install_bridge(
    prompt_server_class: type,
    comfy_port: int,
    *,
    environ: Mapping[str, str] | None = None,
    udp_socket: socket.socket | Any | None = None,
    instance_id: str | uuid.UUID | None = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Mirror supported events after preserving ComfyUI's original delivery."""
    current = prompt_server_class.send_sync
    if getattr(current, "_comfy_progress_bridge", False):
        return False

    target = resolve_target(comfy_port, environ)
    endpoint_host = resolve_endpoint_host(environ)
    sender = udp_socket or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    process_instance_id = uuid.UUID(str(instance_id)) if instance_id else PROCESS_INSTANCE_ID
    sequence = 0
    send_lock = threading.Lock()

    def send_sync_with_bridge(self, event, data, sid=None):
        nonlocal sequence
        result = current(self, event, data, sid)
        try:
            if event in SUPPORTED_EVENTS:
                with send_lock:
                    sequence += 1
                    payload = compact_event(
                        event,
                        data,
                        endpoint_port=comfy_port,
                        endpoint_host=endpoint_host,
                        instance_id=process_instance_id,
                        sequence=sequence,
                        observed_at=clock(),
                    )
                    if payload is not None:
                        sender.sendto(payload, target)
        except Exception:
            # Monitoring is best-effort and must never block ComfyUI execution.
            pass
        return result

    send_sync_with_bridge._comfy_progress_bridge = True  # type: ignore[attr-defined]
    prompt_server_class.send_sync = send_sync_with_bridge
    return True
