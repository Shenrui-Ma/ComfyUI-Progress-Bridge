import json
import math
import runpy
import subprocess
import sys
import threading
import types
from pathlib import Path
from uuid import UUID

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
        self.blocking = None

    def setblocking(self, blocking):
        self.blocking = blocking

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
    assert bridge_port(8189) == 30999
    assert bridge_port(9189) == 30999


def test_bridge_port_warns_when_ignored_base_port_is_supplied():
    with pytest.warns(DeprecationWarning, match="base_port"):
        assert bridge_port(8189, 40000) == 30999


def test_resolve_target_defaults_to_loopback_and_allows_port_override():
    assert resolve_target(8189, {}) == ("127.0.0.1", 30999)
    assert resolve_target(8189, {"COMFY_PROGRESS_BRIDGE_PORT": "41000"}) == (
        "127.0.0.1",
        41000,
    )


def test_resolve_target_rejects_hostnames_to_avoid_event_path_dns():
    with pytest.raises(ValueError, match="numeric IPv4"):
        resolve_target(8189, {"COMFY_PROGRESS_BRIDGE_HOST": "monitor.example.com"})


def test_repository_root_loads_as_an_isolated_comfyui_custom_node(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = f"""
import importlib.util
import importlib.abc
import os
import sys
import types

os.environ['COMFY_PROGRESS_BRIDGE_AUTOSTART'] = '0'
os.environ['COMFYUI_PROGRESS_CONFIG_DIR'] = {str(tmp_path)!r}
comfy = types.ModuleType('comfy')
cli_args = types.ModuleType('comfy.cli_args')
cli_args.args = types.SimpleNamespace(port=8189)
comfy.cli_args = cli_args
server = types.ModuleType('server')
class PromptServer:
    def send_sync(self, event, data, sid=None):
        return None
server.PromptServer = PromptServer
sys.modules['comfy'] = comfy
sys.modules['comfy.cli_args'] = cli_args
sys.modules['server'] = server
class BlockTopLevelPackage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'comfyui_progress_bridge' or fullname.startswith('comfyui_progress_bridge.'):
            raise ModuleNotFoundError(fullname)
        return None
sys.meta_path.insert(0, BlockTopLevelPackage())
root = {str(root)!r}
spec = importlib.util.spec_from_file_location(
    'isolated_custom_node',
    root + '/__init__.py',
    submodule_search_locations=[root],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.WEB_DIRECTORY == './comfyui_progress_bridge/web'
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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
    decoded = json.loads(payload)
    assert decoded["type"] == "progress"
    assert decoded["data"] == {
        "prompt_id": "abc",
        "node": "7",
        "value": 3,
        "max": 10,
    }
    assert decoded["schema"] == 2


def test_two_http_ports_share_target_but_have_collision_free_envelopes():
    first = FakeSocket()
    second = FakeSocket()

    class FirstServer(FakePromptServer):
        pass

    class SecondServer(FakePromptServer):
        pass

    assert install_bridge(
        FirstServer,
        8189,
        udp_socket=first,
        instance_id="00000000-0000-0000-0000-000000000001",
    ) is True
    assert install_bridge(
        SecondServer,
        9189,
        udp_socket=second,
        instance_id="00000000-0000-0000-0000-000000000002",
    ) is True
    FirstServer().send_sync("executing", {"prompt_id": "same", "node": "1"})
    FirstServer().send_sync("progress", {"prompt_id": "same", "node": "1", "value": 1})
    SecondServer().send_sync("execution_success", {"prompt_id": "same"})

    one, one_next = (json.loads(item[0]) for item in first.sent)
    two = json.loads(second.sent[0][0])
    assert first.sent[0][1] == second.sent[0][1] == ("127.0.0.1", 30999)
    assert one["schema"] == two["schema"] == 2
    assert one["endpoint"] == {"host": "127.0.0.1", "port": 8189}
    assert two["endpoint"] == {"host": "127.0.0.1", "port": 9189}
    assert UUID(one["instance_id"]) != UUID(two["instance_id"])
    assert one_next["sequence"] == one["sequence"] + 1
    assert isinstance(one["observed_at"], float)
    assert two["type"] == "execution_success"


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node", 7),
        ("node_id", ["7"]),
        ("display_node", {"name": "7"}),
        ("node_type", object()),
        ("exception_message", RuntimeError("bad")),
        ("value", True),
        ("max", False),
        ("value", [1]),
        ("max", object()),
        ("value", math.nan),
        ("max", math.inf),
    ],
)
def test_compact_event_omits_fields_with_invalid_schema_types(field, value):
    payload = compact_event("progress", {"prompt_id": "abc", field: value})
    assert payload is not None
    assert field not in json.loads(payload)["data"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint_port": 0},
        {"endpoint_port": True},
        {"sequence": -1},
        {"sequence": True},
        {"instance_id": "not-a-uuid"},
        {"observed_at": math.nan},
        {"observed_at": True},
    ],
)
def test_compact_event_rejects_invalid_envelope_fields(kwargs):
    assert compact_event("executing", {"prompt_id": "abc"}, **kwargs) is None


def test_compact_event_ignores_unsupported_or_unscoped_events():
    assert compact_event("status", {"prompt_id": "abc"}) is None
    assert compact_event("progress", {"node": "7", "value": 1, "max": 2}) is None


def test_compact_event_safely_rejects_an_unhashable_event_type():
    assert compact_event([], {"prompt_id": "abc"}) is None


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
    assert FakePromptServer.calls[0][2] == "frontend-client"
    payload, target = sock.sent[-1]
    assert target == ("127.0.0.1", 30999)
    assert json.loads(payload)["data"]["value"] == 3
    assert sock.blocking is False


def test_install_bridge_preserves_comfyui_client_isolation():
    class Server:
        calls = []

        def send_sync(self, event, data, sid=None):
            self.calls.append((event, data, sid))
            return "original-result"

    sock = FakeSocket()
    install_bridge(
        Server,
        8189,
        udp_socket=sock,
        instance_id="00000000-0000-0000-0000-000000000009",
        clock=lambda: 42.5,
    )

    Server().send_sync(
        "progress",
        {
            "prompt_id": "abc",
            "node": "7",
            "value": 3,
            "max": 10,
            "workflow": {"must": "not leak"},
        },
        "submitting-client",
    )

    assert Server.calls == [
        (
            "progress",
            {
                "prompt_id": "abc",
                "node": "7",
                "value": 3,
                "max": 10,
                "workflow": {"must": "not leak"},
            },
            "submitting-client",
        )
    ]


def test_install_bridge_calls_original_before_best_effort_mirror():
    order = []

    class OrderedSocket(FakeSocket):
        def sendto(self, payload, target):
            order.append("mirror")
            super().sendto(payload, target)

    class Server:
        def send_sync(self, event, data, sid=None):
            order.append("original")
            return "delivered"

    install_bridge(Server, 8189, udp_socket=OrderedSocket())
    assert Server().send_sync("executing", {"prompt_id": "abc"}) == "delivered"
    assert order == ["original", "mirror"]


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
    monkeypatch.setattr("comfyui_progress_bridge.launch_desktop", lambda port: False)

    namespace = runpy.run_path(str(Path(__file__).parents[1] / "__init__.py"))

    assert namespace["NODE_CLASS_MAPPINGS"] == {}
    assert getattr(Server.send_sync, "_comfy_progress_bridge", False) is True


def test_successful_comfyui_install_launches_desktop_with_actual_port(monkeypatch):
    import comfyui_progress_bridge

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy_module = types.ModuleType("comfy")
    cli_args_module = types.ModuleType("comfy.cli_args")
    cli_args_module.args = types.SimpleNamespace(port=8197)
    comfy_module.cli_args = cli_args_module
    server_module = types.ModuleType("server")
    server_module.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args_module)
    monkeypatch.setitem(sys.modules, "server", server_module)
    launched = []

    assert (
        comfyui_progress_bridge.install_comfyui_bridge(
            desktop_launcher=lambda port: launched.append(port) or True
        )
        is True
    )
    assert launched == [8197]


def test_desktop_launcher_exception_does_not_undo_bridge_install(monkeypatch):
    import comfyui_progress_bridge

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy_module = types.ModuleType("comfy")
    cli_args_module = types.ModuleType("comfy.cli_args")
    cli_args_module.args = types.SimpleNamespace(port=8198)
    comfy_module.cli_args = cli_args_module
    server_module = types.ModuleType("server")
    server_module.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args_module)
    monkeypatch.setitem(sys.modules, "server", server_module)

    def fail(_port):
        raise RuntimeError("desktop failed")

    assert comfyui_progress_bridge.install_comfyui_bridge(desktop_launcher=fail) is True
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


def test_concurrent_mirrors_serialize_sequence_construction_and_send():
    first_in_send = threading.Event()
    release_first = threading.Event()
    second_sent = threading.Event()

    class CoordinatedSocket(FakeSocket):
        def sendto(self, payload, target):
            sequence = json.loads(payload)["sequence"]
            if sequence == 1:
                first_in_send.set()
                assert release_first.wait(2)
            else:
                second_sent.set()
            self.sent.append((payload, target))

    class Server:
        def send_sync(self, event, data, sid=None):
            return "delivered"

    sock = CoordinatedSocket()
    install_bridge(Server, 8189, udp_socket=sock)
    first = threading.Thread(
        target=Server().send_sync, args=("progress", {"prompt_id": "a", "value": 1})
    )
    second = threading.Thread(
        target=Server().send_sync, args=("progress", {"prompt_id": "b", "value": 2})
    )
    first.start()
    assert first_in_send.wait(2)
    second.start()
    assert not second_sent.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert [json.loads(payload)["sequence"] for payload, _ in sock.sent] == [1, 2]


def test_example_receiver_ignores_valid_non_object_json():
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "examples/receive_progress.py"))
    accepts_event = namespace["accepts_event"]
    assert accepts_event([{"schema": 2}]) is False
    assert accepts_event(None) is False
    assert accepts_event({"schema": 2}) is True
