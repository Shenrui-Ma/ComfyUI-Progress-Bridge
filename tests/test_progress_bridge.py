import json
import runpy
import sys
import types
from pathlib import Path

import pytest

from comfyui_progress_bridge.bridge import (
    bridge_port,
    compact_event,
    install_bridge,
    resolve_target,
)


class FakeSocket:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def sendto(self, payload, target):
        if self.fail:
            raise OSError("monitor unavailable")
        self.sent.append((payload, target))


class FakePromptServer:
    calls = []

    def send_sync(self, event, data, sid=None):
        self.calls.append((event, data, sid))
        return "original-result"


def test_bridge_port_is_stable_for_a_comfyui_port():
    assert bridge_port(8189) == 30189
    assert bridge_port(8202) == 30202


def test_resolve_target_defaults_to_loopback_and_allows_port_override():
    assert resolve_target(8189, {}) == ("127.0.0.1", 30189)
    assert resolve_target(8189, {"COMFY_PROGRESS_BRIDGE_PORT": "41000"}) == (
        "127.0.0.1",
        41000,
    )


def test_resolve_target_rejects_hostnames_to_avoid_event_path_dns():
    with pytest.raises(ValueError, match="numeric IPv4"):
        resolve_target(8189, {"COMFY_PROGRESS_BRIDGE_HOST": "monitor.example.com"})


@pytest.mark.parametrize("value", ["0", "65536", "not-a-number"])
def test_resolve_target_rejects_invalid_port_override(value):
    with pytest.raises(ValueError):
        resolve_target(8189, {"COMFY_PROGRESS_BRIDGE_PORT": value})


def test_compact_event_keeps_only_monitor_fields():
    payload = compact_event(
        "progress",
        {
            "prompt_id": "abc",
            "node": "7",
            "value": 3,
            "max": 10,
            "large_internal_value": "do not forward",
        },
    )
    assert json.loads(payload) == {
        "type": "progress",
        "data": {"prompt_id": "abc", "node": "7", "value": 3, "max": 10},
    }


def test_compact_event_bounds_long_error_messages_for_udp():
    payload = compact_event(
        "execution_error",
        {"prompt_id": "abc", "node": "7", "exception_message": "x" * 100_000},
    )
    decoded = json.loads(payload)
    assert len(decoded["data"]["exception_message"]) == 4096
    assert len(payload) < 8192


def test_compact_event_bounds_every_forwarded_string_for_udp():
    payload = compact_event(
        "execution_error",
        {
            "prompt_id": "abc",
            "node": "n" * 100_000,
            "node_type": "t" * 100_000,
            "exception_message": "x" * 100_000,
        },
    )
    assert payload is not None
    decoded = json.loads(payload)
    assert len(decoded["data"]["node"]) == 1024
    assert len(decoded["data"]["node_type"]) == 1024
    assert len(decoded["data"]["exception_message"]) == 4096
    assert len(payload) < 8192


def test_compact_event_drops_an_unreasonably_long_prompt_id():
    assert compact_event("executing", {"prompt_id": "p" * 257, "node": "7"}) is None


def test_compact_event_ignores_unsupported_or_unscoped_events():
    assert compact_event("status", {"prompt_id": "abc"}) is None
    assert compact_event("progress", {"node": "7", "value": 1, "max": 2}) is None


def test_install_bridge_preserves_original_delivery_and_mirrors_event():
    FakePromptServer.calls = []
    sock = FakeSocket()
    assert install_bridge(FakePromptServer, 8189, udp_socket=sock) is True

    server = FakePromptServer()
    result = server.send_sync(
        "progress",
        {"prompt_id": "abc", "node": "7", "value": 3, "max": 10},
        "frontend-client",
    )

    assert result == "original-result"
    assert FakePromptServer.calls[-1][2] == "frontend-client"
    payload, target = sock.sent[-1]
    assert target == ("127.0.0.1", 30189)
    assert json.loads(payload)["data"]["value"] == 3


def test_install_bridge_is_idempotent():
    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    first = FakeSocket()
    second = FakeSocket()
    assert install_bridge(Server, 8202, udp_socket=first) is True
    assert install_bridge(Server, 8202, udp_socket=second) is False
    Server().send_sync("executing", {"prompt_id": "abc", "node": "4"})
    assert len(first.sent) == 1
    assert second.sent == []


def test_comfyui_entrypoint_installs_the_bridge(monkeypatch):
    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy_module = types.ModuleType("comfy")
    cli_args_module = types.ModuleType("comfy.cli_args")
    cli_args_module.args = types.SimpleNamespace(port=8189)
    comfy_module.cli_args = cli_args_module
    server_module = types.ModuleType("server")
    server_module.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args_module)
    monkeypatch.setitem(sys.modules, "server", server_module)

    namespace = runpy.run_path(str(Path(__file__).parents[1] / "__init__.py"))

    assert namespace["NODE_CLASS_MAPPINGS"] == {}
    assert getattr(Server.send_sync, "_comfy_progress_bridge", False) is True


def test_serialization_failure_never_breaks_comfyui_delivery():
    class ExplodingString:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    class Server:
        def send_sync(self, event, data, sid=None):
            return "still-delivered"

    install_bridge(Server, 8189, udp_socket=FakeSocket())
    assert (
        Server().send_sync(
            "execution_error",
            {"prompt_id": "abc", "exception_message": ExplodingString()},
        )
        == "still-delivered"
    )


def test_monitor_failure_never_breaks_comfyui_delivery():
    class Server:
        def send_sync(self, event, data, sid=None):
            return "still-delivered"

    install_bridge(Server, 8189, udp_socket=FakeSocket(fail=True))
    assert (
        Server().send_sync("executing", {"prompt_id": "abc", "node": "4"})
        == "still-delivered"
    )
