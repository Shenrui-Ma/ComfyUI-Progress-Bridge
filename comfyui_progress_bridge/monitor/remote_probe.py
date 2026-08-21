"""Concurrent HTTP queue probe and shared schema-v2 UDP event receiver."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, TextIO
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

SCHEMA_VERSION = 2
PROTOCOL_VERSION = "2"
DEFAULT_UDP_PORT = 30999
MAX_DATAGRAM_BYTES = 8192
MAX_NDJSON_BYTES = 65_536
MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_QUEUE_ITEMS = 65_536
MAX_WORKFLOW_NODES_TO_INSPECT = 4096
MAX_WORKFLOW_NODES_PER_PROMPT = 3
TERMINAL_EVENTS = frozenset(
    {"execution_success", "execution_error", "execution_interrupted"}
)
EVENT_TYPES = frozenset({"executing", "progress", *TERMINAL_EVENTS})
EVENT_STRING_FIELDS = frozenset(
    {"node", "node_id", "display_node", "node_type", "exception_message"}
)
EVENT_DATA_FIELDS = frozenset({"prompt_id", "value", "max", *EVENT_STRING_FIELDS})
EVENT_ENVELOPE_FIELDS = frozenset(
    {"schema", "endpoint", "instance_id", "sequence", "observed_at", "type", "data"}
)


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and (not isinstance(value, int) or value.bit_length() <= 1024)
        and math.isfinite(value)
    )


def _valid_event_data(event_type: object, data: object) -> bool:
    if event_type not in EVENT_TYPES or not isinstance(data, dict):
        return False
    if not set(data).issubset(EVENT_DATA_FIELDS):
        return False
    prompt_id = data.get("prompt_id")
    if not isinstance(prompt_id, str) or not 0 < len(prompt_id) <= 1024:
        return False
    for key in EVENT_STRING_FIELDS:
        if key in data:
            maximum = 4096 if key == "exception_message" else 1024
            if not isinstance(data[key], str) or len(data[key]) > maximum:
                return False
    for key in ("value", "max"):
        if key in data and not _finite_number(data[key]):
            return False
    return event_type != "progress" or (
        "value" in data and "max" in data and data["value"] >= 0 and data["max"] >= 0
    )


@dataclass(frozen=True, order=True)
class ProbeEndpoint:
    """A numeric ComfyUI HTTP endpoint, safe to use without event-path DNS."""

    host: str
    port: int

    def __post_init__(self) -> None:
        try:
            normalized = str(ipaddress.IPv4Address(self.host))
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError("endpoint host must be a numeric IPv4 address") from exc
        if normalized != self.host:
            object.__setattr__(self, "host", normalized)
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("endpoint port must be an integer between 1 and 65535")


class _LimitedHeaderFile:
    """An unbuffered socket file that bounds bytes consumed by header parsing."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._header_bytes = 0
        self._parsing_headers = True

    def finish_headers(self) -> None:
        self._parsing_headers = False

    def _account(self, data: bytes) -> bytes:
        if self._parsing_headers:
            self._header_bytes += len(data)
            if self._header_bytes > MAX_HTTP_HEADER_BYTES:
                raise ValueError("queue response headers exceed byte limit")
        return data

    def readline(self, size: int = -1) -> bytes:
        return self._account(self._stream.readline(size))

    def read(self, size: int = -1) -> bytes:
        return self._account(self._stream.read(size))

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class _ResponseSocket:
    """Give HTTPResponse an exact-reading, header-limited socket file."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.file: _LimitedHeaderFile | None = None

    def makefile(self, _mode: str, _buffering: int | None = None) -> _LimitedHeaderFile:
        # Avoid buffered read-ahead: body bytes must not count against the header cap.
        self.file = _LimitedHeaderFile(self.sock.makefile("rb", buffering=0))
        return self.file


def _default_http_get(host: str, port: int, timeout: float) -> Any:
    """GET /queue with one monotonic deadline for every blocking HTTP phase."""
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    expired = threading.Event()

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0 or expired.is_set():
            raise TimeoutError("queue response deadline exceeded")
        return budget

    def interrupt() -> None:
        # Socket timeouts are inactivity timeouts. A peer dripping one header byte
        # at a time can defeat them, so forcibly interrupt the fd at the deadline.
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), interrupt)
    watchdog.name = "queue-http-deadline"
    watchdog.daemon = True
    response: http.client.HTTPResponse | None = None
    watchdog.start()
    try:
        # AF_INET plus ProbeEndpoint's numeric validation guarantees no DNS lookup.
        sock.settimeout(remaining())
        sock.connect((host, port))
        request = (
            f"GET /queue HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Accept: application/json\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        sock.settimeout(remaining())
        sock.sendall(request)

        adapter = _ResponseSocket(sock)
        response = http.client.HTTPResponse(adapter)
        sock.settimeout(remaining())
        response.begin()
        assert adapter.file is not None
        adapter.file.finish_headers()
        if response.status != 200:
            raise ValueError(f"queue response has HTTP status {response.status}")

        chunks: list[bytes] = []
        byte_count = 0
        while True:
            sock.settimeout(remaining())
            chunk = response.read(min(65_536, MAX_HTTP_RESPONSE_BYTES + 1 - byte_count))
            remaining()
            if not chunk:
                break
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > MAX_HTTP_RESPONSE_BYTES:
                raise ValueError("queue response exceeds byte limit")
        result = json.loads(b"".join(chunks))
        remaining()
        return result
    except Exception as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("queue response deadline exceeded") from exc
        raise
    finally:
        watchdog.cancel()
        watchdog.join()
        if response is not None:
            response.close()
        sock.close()


def _prompt_id(item: object) -> str | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    value = item[1]
    return value if isinstance(value, str) and 0 < len(value) <= 1024 else None


def _compact_workflow(item: object) -> tuple[dict[str, dict[str, str]], int]:
    if not isinstance(item, (list, tuple)) or len(item) < 3 or not isinstance(item[2], dict):
        return {}, 0
    workflow = item[2]
    # Refuse to walk or sort attacker-controlled mappings beyond a fixed cap.
    # Queue membership is retained; only optional workflow metadata is omitted.
    if len(workflow) > MAX_WORKFLOW_NODES_TO_INSPECT:
        return {}, len(workflow)
    compact: dict[str, dict[str, str]] = {}
    for node_id, node in sorted(workflow.items(), key=lambda pair: str(pair[0])):
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > 1024
            or not isinstance(node, dict)
        ):
            continue
        node_type = node.get("class_type")
        meta = node.get("_meta")
        title = meta.get("title") if isinstance(meta, dict) else None
        fields: dict[str, str] = {}
        if isinstance(node_type, str) and len(node_type) <= 1024:
            fields["node_type"] = node_type
        if isinstance(title, str) and len(title) <= 1024:
            fields["display_node"] = title
        if fields:
            compact[node_id] = fields
    kept = dict(list(compact.items())[:MAX_WORKFLOW_NODES_PER_PROMPT])
    return kept, len(compact) - len(kept)


def _probe_instance_id(endpoint: ProbeEndpoint) -> UUID:
    """Return the stable generation used until the endpoint identifies itself."""
    return uuid5(NAMESPACE_URL, f"comfyui-progress-bridge://{endpoint.host}:{endpoint.port}")


def _status_identity(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return a canonical optional identity for an oversized-record status."""
    try:
        raw_endpoint = record["endpoint"]
        raw_instance_id = record["instance_id"]
        if not isinstance(raw_endpoint, dict) or not isinstance(raw_instance_id, str):
            raise TypeError
        endpoint = ProbeEndpoint(raw_endpoint["host"], raw_endpoint["port"])
        instance_id = UUID(raw_instance_id)
    except (KeyError, TypeError, ValueError):
        return None, None
    return {"host": endpoint.host, "port": endpoint.port}, str(instance_id)


class RemoteProbe:
    """Emit NDJSON snapshots/events for a fixed set of numeric endpoints."""

    def __init__(
        self,
        endpoints: list[ProbeEndpoint] | tuple[ProbeEndpoint, ...],
        *,
        output: TextIO = sys.stdout,
        socket_factory: Any = None,
        http_get: Any = _default_http_get,
        request_timeout: float = 2.0,
        poll_interval: float = 2.0,
    ) -> None:
        if not endpoints or any(not isinstance(item, ProbeEndpoint) for item in endpoints):
            raise ValueError("at least one ProbeEndpoint is required")
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("duplicate endpoints are not allowed")
        if (
            not _finite_number(request_timeout)
            or not _finite_number(poll_interval)
            or request_timeout <= 0
            or poll_interval <= 0
        ):
            raise ValueError("timeouts must be positive")
        self.endpoints = tuple(endpoints)
        self._addresses = {(item.host, item.port): item for item in endpoints}
        self.output = output
        self.socket_factory = socket_factory or (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        )
        self.http_get = http_get
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self._socket: Any | None = None
        self._write_lock = threading.Lock()
        self._endpoint_locks = {endpoint: threading.RLock() for endpoint in endpoints}
        self._instance_ids = {endpoint: _probe_instance_id(endpoint) for endpoint in endpoints}
        self._stop = threading.Event()

    def _emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len((line + "\n").encode("utf-8")) > MAX_NDJSON_BYTES:
            # Never make the consumer silently discard a physical line. This fallback
            # contains no attacker-controlled bulk fields and is itself bounded.
            endpoint, instance_id = _status_identity(record)
            record_kind = record.get("kind")
            line = json.dumps(
                {
                    "kind": "status",
                    "schema": SCHEMA_VERSION,
                    "code": "record_too_large",
                    "record_kind": (
                        record_kind[:128]
                        if isinstance(record_kind, str) and record_kind
                        else "unknown"
                    ),
                    "endpoint": endpoint,
                    "instance_id": instance_id,
                },
                separators=(",", ":"),
            )
        with self._write_lock:
            self.output.write(line + "\n")
            self.output.flush()

    def open(self) -> None:
        if self._socket is not None:
            return
        sock = self.socket_factory()
        try:
            sock.bind(("127.0.0.1", DEFAULT_UDP_PORT))
            sock.settimeout(0.2)
            self._socket = sock
            self._stop.clear()
            self._emit({"kind": "hello", "schema": SCHEMA_VERSION, "version": PROTOCOL_VERSION})
        except BaseException:
            self._socket = None
            sock.close()
            raise

    def close(self) -> None:
        self._stop.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def poll_endpoint(self, endpoint: ProbeEndpoint) -> None:
        with self._endpoint_locks[endpoint]:
            self._poll_endpoint_locked(endpoint)

    def _poll_endpoint_locked(self, endpoint: ProbeEndpoint) -> None:
        observed_at = time.time()
        base: dict[str, Any] = {
            "kind": "snapshot",
            "schema": SCHEMA_VERSION,
            "endpoint": {"host": endpoint.host, "port": endpoint.port},
            "instance_id": str(self._instance_ids[endpoint]),
            "observed_at": observed_at,
        }
        try:
            queue = self.http_get(endpoint.host, endpoint.port, self.request_timeout)
            if not isinstance(queue, dict):
                raise ValueError("queue response is not an object")
            running_items = queue.get("queue_running")
            pending_items = queue.get("queue_pending")
            if not isinstance(running_items, list) or not isinstance(pending_items, list):
                raise ValueError("queue response has invalid lists")
            if len(running_items) + len(pending_items) > MAX_QUEUE_ITEMS:
                raise ValueError("queue response exceeds item limit")

            # One prompt occurrence per chunk means each physical record stays
            # bounded regardless of queue length. Workflow metadata is capped per
            # prompt, but every valid prompt ID is always represented.
            entries: list[tuple[str, str, dict[str, dict[str, str]], int]] = []
            for state, items in (("running", running_items), ("pending", pending_items)):
                for item in items:
                    prompt_id = _prompt_id(item)
                    if prompt_id is None:
                        continue
                    workflow, omitted_nodes = _compact_workflow(item)
                    entries.append((state, prompt_id, workflow, omitted_nodes))
            if not entries:
                entries.append(("empty", "", {}, 0))

            snapshot_id = str(uuid4())
            chunk_count = len(entries)
            for chunk_index, (state, prompt_id, workflow, omitted_nodes) in enumerate(entries):
                chunk = dict(base)
                chunk.update(
                    kind="snapshot_chunk",
                    online=True,
                    snapshot_id=snapshot_id,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    running_prompt_ids=[prompt_id] if state == "running" else [],
                    pending_prompt_ids=[prompt_id] if state == "pending" else [],
                    workflows={prompt_id: workflow} if workflow else {},
                    workflow_truncated_prompt_ids=[prompt_id] if omitted_nodes else [],
                    truncated={
                        "running_prompt_ids": 0,
                        "pending_prompt_ids": 0,
                        "workflow_nodes": omitted_nodes,
                    },
                )
                self._emit(chunk)
            return
        except Exception:
            # An unreachable endpoint is information, never an empty queue.
            base.update(
                online=False,
                running_prompt_ids=[],
                pending_prompt_ids=[],
                workflows={},
                workflow_truncated_prompt_ids=[],
                truncated={
                    "running_prompt_ids": 0,
                    "pending_prompt_ids": 0,
                    "workflow_nodes": 0,
                },
            )
        self._emit(base)

    def poll_all(self) -> None:
        # Each endpoint has its own worker. A dead HTTP port cannot hold up a live record.
        # Daemon workers are intentional: injected HTTP implementations are not
        # trusted to honor their timeout, and must never keep process shutdown alive.
        workers = [
            threading.Thread(
                target=self.poll_endpoint,
                args=(endpoint,),
                name=f"queue-probe-{endpoint.host}:{endpoint.port}",
                daemon=True,
            )
            for endpoint in self.endpoints
        ]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + self.request_timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def handle_datagram(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or len(payload) > MAX_DATAGRAM_BYTES:
            return
        try:
            record = json.loads(payload.decode("utf-8"))
            endpoint_data = record["endpoint"]
            address = (endpoint_data["host"], endpoint_data["port"])
            raw_instance_id = record["instance_id"]
            if not isinstance(raw_instance_id, str):
                return
            instance_id = UUID(raw_instance_id)
            valid = (
                isinstance(record, dict)
                and set(record) == EVENT_ENVELOPE_FIELDS
                and record.get("schema") == SCHEMA_VERSION
                and isinstance(endpoint_data, dict)
                and set(endpoint_data) == {"host", "port"}
                and address in self._addresses
                and record.get("type") in EVENT_TYPES
                and isinstance(record.get("sequence"), int)
                and not isinstance(record.get("sequence"), bool)
                and 0 <= record["sequence"] <= (1 << 63) - 1
                and _finite_number(record.get("observed_at"))
                and record["observed_at"] >= 0
                and _valid_event_data(record.get("type"), record.get("data"))
            )
            if not valid:
                return
        except (
            AttributeError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return
        endpoint = self._addresses[address]
        with self._endpoint_locks[endpoint]:
            if self._instance_ids[endpoint] != instance_id:
                self._instance_ids[endpoint] = instance_id
                # Establish authoritative queue state for the newly discovered
                # generation before any of its events reach the consumer.
                self._poll_endpoint_locked(endpoint)
            emitted = dict(record)
            emitted["instance_id"] = str(instance_id)
            emitted["kind"] = "event"
            self._emit(emitted)
            if record["type"] in TERMINAL_EVENTS:
                self._poll_endpoint_locked(endpoint)

    def run(self) -> None:
        self.open()
        sock = self._socket
        assert sock is not None
        poller = threading.Thread(target=self._poll_loop, name="queue-poller", daemon=True)
        poller.start()
        try:
            while not self._stop.is_set():
                try:
                    payload, _sender = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self.handle_datagram(payload)
        finally:
            self.close()
            poller.join(timeout=self.request_timeout + 1)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_all()
            remaining = max(0.0, self.poll_interval - (time.monotonic() - started))
            self._stop.wait(remaining)


def _endpoint_argument(value: str) -> ProbeEndpoint:
    try:
        host, raw_port = value.rsplit(":", 1)
        return ProbeEndpoint(host, int(raw_port))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("endpoint must be numeric-IPv4:port") from exc


def main(argv: list[str] | None = None) -> int:
    """Run the standalone probe, writing protocol records to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", nargs="+", type=_endpoint_argument)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    arguments = parser.parse_args(argv)
    probe = RemoteProbe(
        arguments.endpoint,
        poll_interval=arguments.poll_interval,
        request_timeout=arguments.request_timeout,
    )
    try:
        probe.run()
    except KeyboardInterrupt:
        probe.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a deployed entry point
    raise SystemExit(main())
