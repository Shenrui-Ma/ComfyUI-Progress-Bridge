import json
import socket
import threading
import time
from io import StringIO
from uuid import UUID

import pytest

from comfyui_progress_bridge.monitor.remote_probe import (
    MAX_HTTP_HEADER_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_NDJSON_BYTES,
    MAX_QUEUE_ITEMS,
    MAX_WORKFLOW_NODES_TO_INSPECT,
    PROTOCOL_VERSION,
    ProbeEndpoint,
    RemoteProbe,
    _default_http_get,
)
from comfyui_progress_bridge.monitor.source import _valid_record


class FakeSocket:
    def __init__(self):
        self.bound = None
        self.timeout = None
        self.closed = False

    def bind(self, address):
        self.bound = address

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class BlockingHTTP:
    def __init__(self):
        self.release = threading.Event()
        self.started = []
        self.lock = threading.Lock()

    def __call__(self, host, port, timeout):
        with self.lock:
            self.started.append(port)
        if port == 8189:
            self.release.wait(timeout=1)
            raise TimeoutError("dead")
        return {
            "queue_running": [
                [
                    1,
                    "live",
                    {"7": {"class_type": "KSampler", "_meta": {"title": "Sampler"}}},
                ]
            ],
            "queue_pending": [],
        }


def envelope(port=9189, event_type="progress"):
    return {
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": port},
        "instance_id": "00000000-0000-0000-0000-000000000002",
        "sequence": 3,
        "observed_at": 10.0,
        "type": event_type,
        "data": {"prompt_id": "same", "node": "7", "value": 1, "max": 2},
    }


def empty_queue(_host, _port, _timeout):
    return {"queue_running": [], "queue_pending": []}


def start_http_server(handler):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        try:
            connection, _address = server.accept()
            with connection:
                connection.recv(4096)
                handler(connection)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            server.close()

    thread = threading.Thread(target=serve, name="test-http-server", daemon=True)
    thread.start()
    return server.getsockname()[1], thread


def test_probe_uses_one_loopback_socket_and_emits_versioned_hello():
    output = StringIO()
    sock = FakeSocket()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8189)], output=output, socket_factory=lambda: sock
    )
    probe.open()

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert sock.bound == ("127.0.0.1", 30999)
    assert records == [{"kind": "hello", "schema": 2, "version": PROTOCOL_VERSION}]
    probe.close()
    assert sock.closed


def test_events_are_schema_v2_only_and_demultiplexed_by_configured_envelope_endpoint():
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8189), ProbeEndpoint("127.0.0.1", 9189)],
        output=output,
        socket_factory=FakeSocket,
        http_get=empty_queue,
    )
    probe.open()
    probe.handle_datagram(json.dumps(envelope()).encode())
    invalid = envelope(8189)
    invalid["schema"] = 1
    probe.handle_datagram(json.dumps(invalid).encode())
    probe.handle_datagram(json.dumps(envelope(9999)).encode())
    probe.handle_datagram(b"not json")

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["kind"] for record in records] == ["hello", "snapshot_chunk", "event"]
    assert records[1]["instance_id"] == records[2]["instance_id"]
    assert records[2]["endpoint"]["port"] == 9189
    assert UUID(records[2]["instance_id"]).int == 2


def test_probe_rejects_event_envelopes_with_unknown_top_level_fields():
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 9189)], output=output, http_get=empty_queue
    )
    record = envelope()
    record["unexpected"] = True

    probe.handle_datagram(json.dumps(record).encode())

    assert output.getvalue() == ""


def test_queue_polls_are_concurrent_and_failure_is_explicit_offline_not_empty():
    output = StringIO()
    http = BlockingHTTP()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8189), ProbeEndpoint("127.0.0.1", 9189)],
        output=output,
        socket_factory=FakeSocket,
        http_get=http,
    )
    probe.open()
    worker = threading.Thread(target=probe.poll_all)
    worker.start()

    deadline = time.monotonic() + 0.5
    while 9189 not in http.started and time.monotonic() < deadline:
        time.sleep(0.001)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    live = [
        r
        for r in records
        if r.get("kind") == "snapshot_chunk" and r["endpoint"]["port"] == 9189
    ]
    assert live and live[0]["online"] is True
    assert live[0]["running_prompt_ids"] == ["live"]
    assert live[0]["workflows"]["live"]["7"] == {
        "node_type": "KSampler",
        "display_node": "Sampler",
    }

    http.release.set()
    worker.join(timeout=1)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    dead = [r for r in records if r.get("kind") == "snapshot" and r["endpoint"]["port"] == 8189]
    assert dead == [
        {
            "kind": "snapshot",
            "schema": 2,
            "endpoint": {"host": "127.0.0.1", "port": 8189},
            "instance_id": dead[0]["instance_id"],
            "observed_at": dead[0]["observed_at"],
            "online": False,
            "running_prompt_ids": [],
            "pending_prompt_ids": [],
            "workflows": {},
            "workflow_truncated_prompt_ids": [],
            "truncated": {
                "running_prompt_ids": 0,
                "pending_prompt_ids": 0,
                "workflow_nodes": 0,
            },
        }
    ]
    UUID(dead[0]["instance_id"])


def test_terminal_event_immediately_reconciles_only_same_endpoint():
    calls = []

    def get(host, port, timeout):
        calls.append((host, port))
        return {"queue_running": [], "queue_pending": []}

    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8189), ProbeEndpoint("127.0.0.1", 9189)],
        output=StringIO(),
        socket_factory=FakeSocket,
        http_get=get,
    )
    probe.open()
    probe.handle_datagram(json.dumps(envelope(9189, "execution_success")).encode())
    assert calls == [("127.0.0.1", 9189), ("127.0.0.1", 9189)]


def test_initial_snapshots_have_stable_per_endpoint_probe_generations():
    first, second = ProbeEndpoint("127.0.0.1", 8189), ProbeEndpoint("127.0.0.1", 9189)
    output = StringIO()
    probe = RemoteProbe([first, second], output=output, http_get=empty_queue)
    probe.poll_all()
    probe.poll_all()
    snapshots = [json.loads(line) for line in output.getvalue().splitlines()]
    ids = {(item["endpoint"]["port"], item["instance_id"]) for item in snapshots}
    assert len(ids) == 2
    assert all(UUID(instance_id) for _, instance_id in ids)


def test_malformed_uuid_types_are_dropped_without_poll_or_emit():
    output = StringIO()
    calls = []

    def get(*args):
        calls.append(args)
        return {"queue_running": [], "queue_pending": []}

    probe = RemoteProbe([ProbeEndpoint("127.0.0.1", 9189)], output=output, http_get=get)
    for malformed in (None, 1, True, [], {}):
        record = envelope()
        record["instance_id"] = malformed
        probe.handle_datagram(json.dumps(record).encode())
    assert output.getvalue() == ""
    assert calls == []


def test_snapshot_payload_is_deterministically_truncated_and_lines_are_bounded():
    huge = "\U0001f600" * 1024
    workflow = {
        str(index): {"class_type": huge, "_meta": {"title": huge}} for index in range(20)
    }
    queue = {
        "queue_running": [[index, f"running-{index}", workflow] for index in range(10)],
        "queue_pending": [[index, f"pending-{index}", workflow] for index in range(10)],
    }
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8189)], output=output, http_get=lambda *_args: queue
    )
    probe.poll_endpoint(probe.endpoints[0])
    physical = output.getvalue().splitlines(keepends=True)
    assert all(len(line.encode()) <= MAX_NDJSON_BYTES for line in physical)
    record = json.loads(physical[0])
    assert record["running_prompt_ids"] == ["running-0"]
    assert record["pending_prompt_ids"] == []
    assert len(physical) == 20
    assert [
        json.loads(line)["running_prompt_ids"] + json.loads(line)["pending_prompt_ids"]
        for line in physical
    ] == [[f"running-{index}"] for index in range(10)] + [
        [f"pending-{index}"] for index in range(10)
    ]
    assert record["truncated"] == {
        "running_prompt_ids": 0,
        "pending_prompt_ids": 0,
        "workflow_nodes": 17,
    }


def test_unexpected_oversized_record_becomes_explicit_bounded_status():
    output = StringIO()
    probe = RemoteProbe([ProbeEndpoint("127.0.0.1", 8189)], output=output)
    probe._emit({"kind": "snapshot", "bulk": "x" * MAX_NDJSON_BYTES})
    line = output.getvalue()
    assert len(line.encode()) <= MAX_NDJSON_BYTES
    assert json.loads(line) == {
        "kind": "status",
        "schema": 2,
        "code": "record_too_large",
        "record_kind": "snapshot",
        "endpoint": None,
        "instance_id": None,
    }
    assert _valid_record(json.loads(line))


def test_all_probe_emitted_record_kinds_validate_as_source_protocol(monkeypatch):
    # This checks record shapes, not the public UDP port; coexist with a running dock.
    monkeypatch.setattr("comfyui_progress_bridge.monitor.remote_probe.DEFAULT_UDP_PORT", 0)
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 9189)], output=output, http_get=empty_queue
    )
    probe.open()
    probe.poll_endpoint(probe.endpoints[0])
    probe.handle_datagram(json.dumps(envelope()).encode())
    probe._emit({"kind": "snapshot", "bulk": "x" * MAX_NDJSON_BYTES})

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert {record["kind"] for record in records} == {
        "hello",
        "snapshot_chunk",
        "event",
        "status",
    }
    assert all(_valid_record(record) for record in records)
    probe.close()


def test_http_reader_is_byte_bounded():
    def oversized(connection):
        connection.sendall(
            f"HTTP/1.1 200 OK\r\nContent-Length: {MAX_HTTP_RESPONSE_BYTES + 1}\r\n\r\n".encode()
        )
        block = b"x" * 65_536
        for _offset in range(0, MAX_HTTP_RESPONSE_BYTES + 1, len(block)):
            connection.sendall(block)

    port, server_thread = start_http_server(oversized)

    with pytest.raises(ValueError, match="byte limit"):
        _default_http_get("127.0.0.1", port, 2.0)

    server_thread.join(timeout=1)
    assert not server_thread.is_alive()


def test_http_reader_keeps_a_total_header_byte_limit():
    def oversized_headers(connection):
        headers = b"".join(
            f"X-{index}: ".encode() + b"x" * 1100 + b"\r\n" for index in range(60)
        )
        assert len(headers) > MAX_HTTP_HEADER_BYTES
        connection.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n{}")

    port, server_thread = start_http_server(oversized_headers)

    with pytest.raises(ValueError, match="headers exceed byte limit"):
        _default_http_get("127.0.0.1", port, 1.0)

    server_thread.join(timeout=1)
    assert not server_thread.is_alive()


def test_http_reader_enforces_total_deadline_against_real_slow_header_drip():
    def drip_headers(connection):
        connection.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
        # Every byte arrives inside the socket inactivity timeout, but the header
        # never completes inside the total request deadline.
        for _index in range(100):
            connection.sendall(b"x")
            time.sleep(0.02)

    port, server_thread = start_http_server(drip_headers)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="deadline"):
        _default_http_get("127.0.0.1", port, 0.15)

    elapsed = time.monotonic() - started
    assert 0.10 <= elapsed < 0.6
    server_thread.join(timeout=1)
    assert not server_thread.is_alive()


def test_poll_and_run_shutdown_never_retain_non_daemon_http_workers():
    entered = threading.Event()
    release = threading.Event()

    def stuck_http(*_args):
        entered.set()
        release.wait()
        raise TimeoutError

    class RunSocket(FakeSocket):
        def __init__(self):
            super().__init__()
            self.closed_event = threading.Event()

        def recvfrom(self, _size):
            self.closed_event.wait()
            raise OSError("closed")

        def close(self):
            super().close()
            self.closed_event.set()

    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8188)],
        output=StringIO(),
        socket_factory=RunSocket,
        http_get=stuck_http,
        request_timeout=0.05,
        poll_interval=1,
    )
    runner = threading.Thread(target=probe.run, name="test-probe-runner")
    runner.start()
    assert entered.wait(timeout=0.5)
    started = time.monotonic()
    probe.close()
    runner.join(timeout=0.5)
    elapsed = time.monotonic() - started

    live_probe_workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(("queue-probe-", "queue-poller"))
    ]
    assert not runner.is_alive()
    assert elapsed < 0.5
    assert all(thread.daemon for thread in live_probe_workers)
    release.set()
    for thread in live_probe_workers:
        thread.join(timeout=0.5)


def test_queue_over_item_cap_fails_whole_snapshot_instead_of_truncating_ids():
    item = [0, "task", {}]
    queue = {"queue_running": [item] * (MAX_QUEUE_ITEMS + 1), "queue_pending": []}
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8188)], output=output, http_get=lambda *_args: queue
    )

    probe.poll_all()

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(records) == 1
    assert records[0]["kind"] == "snapshot"
    assert records[0]["online"] is False
    assert records[0]["running_prompt_ids"] == []


def test_oversized_workflow_is_not_walked_but_prompt_id_is_preserved():
    workflow = {
        str(index): {"class_type": "Node"}
        for index in range(MAX_WORKFLOW_NODES_TO_INSPECT + 1)
    }
    output = StringIO()
    probe = RemoteProbe(
        [ProbeEndpoint("127.0.0.1", 8188)],
        output=output,
        http_get=lambda *_args: {
            "queue_running": [[0, "preserved", workflow]],
            "queue_pending": [],
        },
    )

    probe.poll_all()

    record = json.loads(output.getvalue())
    assert record["online"] is True
    assert record["running_prompt_ids"] == ["preserved"]
    assert record["workflows"] == {}
    assert record["workflow_truncated_prompt_ids"] == ["preserved"]
    assert record["truncated"]["workflow_nodes"] == len(workflow)


@pytest.mark.parametrize(
    "request_timeout,poll_interval",
    [(True, 1), (1, False), (float("inf"), 1), (1, float("nan")), (0, 1)],
)
def test_probe_rejects_non_finite_boolean_or_nonpositive_timing(request_timeout, poll_interval):
    with pytest.raises(ValueError, match="timeouts"):
        RemoteProbe(
            [ProbeEndpoint("127.0.0.1", 8188)],
            request_timeout=request_timeout,
            poll_interval=poll_interval,
        )


@pytest.mark.parametrize("failure", ["bind", "settimeout"])
def test_socket_is_closed_on_every_open_setup_failure(failure):
    class FailingSocket(FakeSocket):
        def bind(self, address):
            super().bind(address)
            if failure == "bind":
                raise OSError("bind failed")

        def settimeout(self, timeout):
            super().settimeout(timeout)
            if failure == "settimeout":
                raise OSError("timeout failed")

    sock = FailingSocket()
    probe = RemoteProbe([ProbeEndpoint("127.0.0.1", 8188)], socket_factory=lambda: sock)
    with pytest.raises(OSError):
        probe.open()
    assert sock.closed
    assert probe._socket is None
