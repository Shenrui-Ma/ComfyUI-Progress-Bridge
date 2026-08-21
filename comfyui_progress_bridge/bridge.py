"""Core implementation for the ComfyUI external progress event bridge."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Mapping
from typing import Any

SUPPORTED_EVENTS = frozenset(
    {"executing", "progress", "execution_error", "execution_interrupted"}
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


def bridge_port(comfy_port: int, base_port: int = 30000) -> int:
    """Map a ComfyUI HTTP port to a deterministic local UDP bridge port."""
    return base_port + (int(comfy_port) % 1000)


def resolve_target(
    comfy_port: int, environ: Mapping[str, str] | None = None
) -> tuple[str, int]:
    """Resolve the UDP destination from environment variables."""
    values = os.environ if environ is None else environ
    host = values.get("COMFY_PROGRESS_BRIDGE_HOST", "127.0.0.1")
    try:
        host = str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError as exc:
        raise ValueError("COMFY_PROGRESS_BRIDGE_HOST must be a numeric IPv4 address") from exc
    raw_port = values.get("COMFY_PROGRESS_BRIDGE_PORT")
    try:
        port = bridge_port(comfy_port) if raw_port is None else int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("COMFY_PROGRESS_BRIDGE_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("COMFY_PROGRESS_BRIDGE_PORT must be between 1 and 65535")
    return host, port


MAX_DATAGRAM_BYTES = 8192
MAX_PROMPT_ID_CHARS = 256
MAX_FIELD_CHARS = 1024
MAX_ERROR_CHARS = 4096


def compact_event(event: str, data: Any) -> bytes | None:
    """Serialize a bounded, non-binary subset needed by an external monitor."""
    if event not in SUPPORTED_EVENTS or not isinstance(data, dict):
        return None
    try:
        compact = {key: data[key] for key in FORWARDED_DATA_KEYS if key in data}
        prompt_id = compact.get("prompt_id")
        if not isinstance(prompt_id, str) or len(prompt_id) > MAX_PROMPT_ID_CHARS:
            return None
        for key, value in tuple(compact.items()):
            if not isinstance(value, str) or key == "prompt_id":
                continue
            limit = MAX_ERROR_CHARS if key == "exception_message" else MAX_FIELD_CHARS
            compact[key] = value[:limit]
        payload = json.dumps(
            {"type": event, "data": compact},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
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
) -> bool:
    """Mirror supported events while preserving ComfyUI's original delivery."""
    current = prompt_server_class.send_sync
    if getattr(current, "_comfy_progress_bridge", False):
        return False

    target = resolve_target(comfy_port, environ)
    sender = udp_socket or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send_sync_with_bridge(self, event, data, sid=None):
        result = current(self, event, data, sid)
        try:
            payload = compact_event(event, data)
            if payload is not None:
                sender.sendto(payload, target)
        except Exception:
            # Monitoring is best-effort and must never block ComfyUI execution.
            pass
        return result

    send_sync_with_bridge._comfy_progress_bridge = True  # type: ignore[attr-defined]
    prompt_server_class.send_sync = send_sync_with_bridge
    return True
