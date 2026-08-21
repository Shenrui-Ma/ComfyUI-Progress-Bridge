"""Restartable local and persistent-SSH NDJSON probe sources."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .models import MAX_PROMPT_ID_LENGTH, MAX_SEQUENCE
from .remote_probe import EVENT_TYPES, PROTOCOL_VERSION, SCHEMA_VERSION

MAX_NDJSON_BYTES = 65_536
MAX_STDERR_CHARS = 4096
MAX_INFLIGHT_SNAPSHOTS = 32
MAX_INFLIGHT_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_CHUNKS = 65_536
SNAPSHOT_ASSEMBLY_TTL = 30.0
MAX_SETTLED_SNAPSHOTS = 128
MAX_PROMPT_IDS_PER_CHUNK = 64
MAX_WORKFLOW_NODES_PER_CHUNK = 64
MAX_PROTOCOL_STRING = 1024
MAX_ERROR_STRING = 4096
_SAFE_ARGUMENT = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_SAFE_SSH_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SAFE_SSH_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_SECRET = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
_EVENT_STRING_FIELDS = frozenset(
    {"node", "node_id", "display_node", "node_type", "exception_message"}
)
_EVENT_DATA_FIELDS = frozenset({"prompt_id", "value", "max", *_EVENT_STRING_FIELDS})
_HELLO_REQUIRED_FIELDS = frozenset({"kind", "schema", "version"})
_SNAPSHOT_REQUIRED_FIELDS = frozenset(
    {
        "kind",
        "schema",
        "endpoint",
        "instance_id",
        "observed_at",
        "online",
        "running_prompt_ids",
        "pending_prompt_ids",
        "workflows",
        "truncated",
    }
)
_SNAPSHOT_OPTIONAL_FIELDS = frozenset({"workflow_truncated_prompt_ids"})
_SNAPSHOT_CHUNK_REQUIRED_FIELDS = _SNAPSHOT_REQUIRED_FIELDS | {
    "snapshot_id",
    "chunk_index",
    "chunk_count",
}
_EVENT_REQUIRED_FIELDS = frozenset(
    {
        "kind",
        "schema",
        "endpoint",
        "instance_id",
        "sequence",
        "observed_at",
        "type",
        "data",
    }
)
_STATUS_FIELD_SETS = {
    "record_too_large": (
        frozenset({"kind", "schema", "code", "record_kind", "endpoint", "instance_id"}),
        frozenset(),
    )
}


def _has_exact_fields(
    record: dict[str, Any], required: frozenset[str], optional: frozenset[str] = frozenset()
) -> bool:
    fields = set(record)
    return required.issubset(fields) and fields.issubset(required | optional)


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int) and value.bit_length() > 1024:
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except (AttributeError, TypeError, ValueError):
        return False


def _valid_endpoint(record: dict[str, Any]) -> bool:
    endpoint = record.get("endpoint")
    if not isinstance(endpoint, dict) or set(endpoint) != {"host", "port"}:
        return False
    host, port = endpoint.get("host"), endpoint.get("port")
    if not isinstance(host, str):
        return False
    try:
        if str(ipaddress.IPv4Address(host)) != host:
            return False
    except (ipaddress.AddressValueError, TypeError):
        return False
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    return _valid_uuid(record.get("instance_id"))


def _valid_prompt_ids(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and 0 < len(item) <= MAX_PROMPT_ID_LENGTH for item in value
    )


def _valid_workflows(value: object, active: set[str], *, bounded_subset: bool) -> bool:
    if not isinstance(value, dict) or not set(value).issubset(active):
        return False
    node_count = 0
    for prompt_id, nodes in value.items():
        if (
            not isinstance(prompt_id, str)
            or not 0 < len(prompt_id) <= MAX_PROMPT_ID_LENGTH
            or not isinstance(nodes, dict)
        ):
            return False
        for node_id, node in nodes.items():
            node_count += 1
            if bounded_subset and node_count > MAX_WORKFLOW_NODES_PER_CHUNK:
                return False
            if not isinstance(node_id, str) or not node_id or len(node_id) > MAX_PROTOCOL_STRING:
                return False
            if not isinstance(node, dict) or not node or not set(node).issubset(
                {"node_type", "display_node"}
            ):
                return False
            if not all(
                isinstance(item, str) and len(item) <= MAX_PROTOCOL_STRING
                for item in node.values()
            ):
                return False
    return True


def _valid_truncation(record: dict[str, Any], active: set[str]) -> bool:
    truncated = record.get("truncated")
    omitted = record.get("workflow_truncated_prompt_ids", [])
    return (
        isinstance(truncated, dict)
        and set(truncated) == {"running_prompt_ids", "pending_prompt_ids", "workflow_nodes"}
        and all(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SEQUENCE
            for value in truncated.values()
        )
        and truncated["running_prompt_ids"] == 0
        and truncated["pending_prompt_ids"] == 0
        and _valid_prompt_ids(omitted)
        and set(omitted).issubset(active)
        and bool(omitted) == (truncated["workflow_nodes"] > 0)
        and truncated["workflow_nodes"] >= len(set(omitted))
    )


def _valid_snapshot(record: dict[str, Any], *, chunk: bool) -> bool:
    required = _SNAPSHOT_CHUNK_REQUIRED_FIELDS if chunk else _SNAPSHOT_REQUIRED_FIELDS
    if not _has_exact_fields(record, required, _SNAPSHOT_OPTIONAL_FIELDS):
        return False
    running = record.get("running_prompt_ids")
    pending = record.get("pending_prompt_ids")
    if not _valid_prompt_ids(running) or not _valid_prompt_ids(pending):
        return False
    assert isinstance(running, list) and isinstance(pending, list)
    active = set((*running, *pending))
    common = (
        _valid_endpoint(record)
        and isinstance(record.get("online"), bool)
        and _finite_number(record.get("observed_at"))
        and record["observed_at"] >= 0
        and _valid_workflows(record.get("workflows"), active, bounded_subset=chunk)
        and _valid_truncation(record, active)
    )
    if not common:
        return False
    if not record["online"]:
        return not chunk and not active and not record["workflows"]
    if chunk:
        index, count = record.get("chunk_index"), record.get("chunk_count")
        return (
            len(running) + len(pending) <= MAX_PROMPT_IDS_PER_CHUNK
            and _valid_uuid(record.get("snapshot_id"))
            and isinstance(index, int)
            and not isinstance(index, bool)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and 1 <= count <= MAX_SNAPSHOT_CHUNKS
            and 0 <= index < count
        )
    return True


def _valid_event_data(event_type: str, data: object) -> bool:
    if not isinstance(data, dict) or not set(data).issubset(_EVENT_DATA_FIELDS):
        return False
    prompt_id = data.get("prompt_id")
    if not isinstance(prompt_id, str) or not 0 < len(prompt_id) <= MAX_PROMPT_ID_LENGTH:
        return False
    for key in _EVENT_STRING_FIELDS:
        if key not in data:
            continue
        value = data[key]
        maximum = MAX_ERROR_STRING if key == "exception_message" else MAX_PROTOCOL_STRING
        if not isinstance(value, str) or len(value) > maximum:
            return False
    for key in ("value", "max"):
        if key in data and not _finite_number(data[key]):
            return False
    return event_type != "progress" or (
        "value" in data and "max" in data and data["value"] >= 0 and data["max"] >= 0
    )


def _valid_record(record: dict[str, Any]) -> bool:
    """Strictly validate records before exposing them to application callbacks."""
    if record.get("schema") != SCHEMA_VERSION or not isinstance(record.get("kind"), str):
        return False
    kind = record["kind"]
    if kind == "hello":
        return _has_exact_fields(record, _HELLO_REQUIRED_FIELDS) and isinstance(
            record.get("version"), str
        )
    if kind in {"snapshot", "snapshot_chunk"}:
        return _valid_snapshot(record, chunk=kind == "snapshot_chunk")
    if kind == "event":
        if not _has_exact_fields(record, _EVENT_REQUIRED_FIELDS):
            return False
        sequence = record.get("sequence")
        event_type = record.get("type")
        return (
            _valid_endpoint(record)
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and 0 <= sequence <= MAX_SEQUENCE
            and _finite_number(record.get("observed_at"))
            and record["observed_at"] >= 0
            and event_type in EVENT_TYPES
            and _valid_event_data(event_type, record.get("data"))
        )
    if kind == "status":
        code = record.get("code")
        field_set = _STATUS_FIELD_SETS.get(code) if isinstance(code, str) else None
        if field_set is None or not _has_exact_fields(record, *field_set):
            return False
        if code == "record_too_large":
            record_kind = record.get("record_kind")
            endpoint = record.get("endpoint")
            instance_id = record.get("instance_id")
            return (
                isinstance(record_kind, str)
                and 0 < len(record_kind) <= 128
                and ((endpoint is None and instance_id is None) or _valid_endpoint(record))
            )
        return False
    return False


def _argv(values: Sequence[str], name: str, *, remote_safe: bool = False) -> list[str]:
    if isinstance(values, (str, bytes)) or not values:
        raise ValueError(f"{name} must be a non-empty argv sequence")
    result = list(values)
    for value in result:
        if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
            raise ValueError(f"{name} contains an invalid argument")
        # OpenSSH invokes a remote shell even though local Popen does not. Restrict
        # remote words so none can become shell syntax after OpenSSH joins them.
        if remote_safe and not _SAFE_ARGUMENT.fullmatch(value):
            raise ValueError(f"{name} contains unsafe remote-shell characters")
    return result


def build_local_argv(command: Sequence[str]) -> list[str]:
    """Validate and copy a local command argv."""
    return _argv(command, "command")


def build_ssh_argv(
    *,
    host: str,
    user: str,
    remote_argv: Sequence[str],
    port: int = 22,
    identity_file: str | None = None,
    ssh_binary: str = "ssh",
) -> list[str]:
    """Build a batch-mode, keepalive SSH argv without passwords or interpolation."""
    if not isinstance(host, str) or host.startswith("-") or not _SAFE_SSH_HOST.fullmatch(host):
        raise ValueError("invalid SSH host")
    if not isinstance(user, str) or user.startswith("-") or not _SAFE_SSH_USER.fullmatch(user):
        raise ValueError("invalid SSH user")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid SSH port")
    remote = _argv(remote_argv, "remote_argv", remote_safe=True)
    argv = [
        *_argv([ssh_binary], "ssh_binary"),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if port != 22:
        argv.extend(["-p", str(port)])
    if identity_file is not None:
        argv.extend(["-i", _argv([identity_file], "identity_file")[0]])
    argv.append(f"{user}@{host}")
    argv.extend(remote)
    return argv


def redact_error(message: object) -> str:
    """Bound and remove common credential assignments from an error message."""
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(message))[
        :MAX_STDERR_CHARS
    ]


def _redact(message: str) -> str:
    return redact_error(message)


@dataclass
class _SnapshotAssembly:
    """A bounded, not-yet-authoritative collection of snapshot chunks."""

    identity: tuple[Any, ...]
    count: int
    created_at: float
    chunks: dict[int, dict[str, Any]] = field(default_factory=dict)
    byte_count: int = 0


class LocalSource:
    """Supervise one line-oriented probe process across failures and restarts."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        on_record: Callable[[dict[str, Any]], None],
        on_error: Callable[[str], None] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        backoff: Sequence[float] = (0.25, 1.0, 2.0, 5.0),
    ) -> None:
        self.argv = build_local_argv(argv)
        if not callable(on_record) or (on_error is not None and not callable(on_error)):
            raise ValueError("callbacks must be callable")
        if (
            isinstance(backoff, (str, bytes))
            or not backoff
            or any(not _finite_number(value) or value < 0 for value in backoff)
        ):
            raise ValueError("backoff must contain non-negative delays")
        self.on_record = on_record
        self.on_error = on_error or (lambda _message: None)
        self._popen = popen
        self._backoff = tuple(float(value) for value in backoff)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any | None = None
        self._lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._assemblies: dict[str, _SnapshotAssembly] = {}
        # Recently completed/rejected IDs prevent duplicate completion and a
        # conflicting chunk from resurrecting a discarded set.
        self._settled_snapshots: dict[str, float] = {}

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="probe-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._stop_process(process)
        self.join(timeout=2)

    def join(self, timeout: float | None = None) -> None:
        if timeout is not None and (not _finite_number(timeout) or timeout < 0):
            raise ValueError("join timeout must be finite and non-negative")
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        try:
            descriptor = stream.fileno()
        except (AttributeError, ValueError, OSError):
            descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _stop_process(self, process: Any) -> None:
        """Bound child shutdown in the caller thread and unblock pipe readers."""
        with self._shutdown_lock:
            try:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:
                        process.kill()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            # A killed real child must be reaped before another launch.
                            process.wait()
                else:
                    # wait() is immediate after poll() and documents the reap invariant.
                    process.wait()
            finally:
                self._close_stream(getattr(process, "stdout", None))
                self._close_stream(getattr(process, "stderr", None))

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _report_stderr(self, stream: Any) -> None:
        try:
            captured = ""
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                if len(captured) < MAX_STDERR_CHARS:
                    captured += str(chunk)[: MAX_STDERR_CHARS - len(captured)]
            if captured:
                self.on_error(_redact(captured))
        except Exception:
            return

    def _purge_snapshot_assemblies(self, now: float) -> None:
        cutoff = now - SNAPSHOT_ASSEMBLY_TTL
        for snapshot_id, assembly in tuple(self._assemblies.items()):
            if assembly.created_at < cutoff:
                self._settle_snapshot(snapshot_id, now)
        for snapshot_id, settled_at in tuple(self._settled_snapshots.items()):
            if settled_at < cutoff:
                self._settled_snapshots.pop(snapshot_id, None)

    def _settle_snapshot(self, snapshot_id: str, now: float) -> None:
        self._assemblies.pop(snapshot_id, None)
        self._settled_snapshots[snapshot_id] = now
        while len(self._settled_snapshots) > MAX_SETTLED_SNAPSHOTS:
            oldest_id = min(
                self._settled_snapshots, key=lambda item: self._settled_snapshots[item]
            )
            self._settled_snapshots.pop(oldest_id, None)

    def _assemble_snapshot_chunk(
        self, record: dict[str, Any], *, byte_count: int
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        self._purge_snapshot_assemblies(now)
        snapshot_id = record["snapshot_id"]
        if snapshot_id in self._settled_snapshots:
            return None
        identity = (
            record["endpoint"]["host"],
            record["endpoint"]["port"],
            record["instance_id"],
            record["observed_at"],
            record["online"],
            record["chunk_count"],
        )
        assembly = self._assemblies.get(snapshot_id)
        if assembly is None:
            while len(self._assemblies) >= MAX_INFLIGHT_SNAPSHOTS:
                oldest_id = min(
                    self._assemblies, key=lambda item: self._assemblies[item].created_at
                )
                self._settle_snapshot(oldest_id, now)
            assembly = _SnapshotAssembly(identity, record["chunk_count"], now)
            self._assemblies[snapshot_id] = assembly
        elif assembly.identity != identity:
            self._settle_snapshot(snapshot_id, now)
            return None

        index = record["chunk_index"]
        existing = assembly.chunks.get(index)
        if existing is not None:
            if existing != record:
                self._settle_snapshot(snapshot_id, now)
            return None

        total_bytes = sum(item.byte_count for item in self._assemblies.values())
        if total_bytes + byte_count > MAX_INFLIGHT_SNAPSHOT_BYTES:
            self._settle_snapshot(snapshot_id, now)
            return None
        assembly.chunks[index] = record
        assembly.byte_count += byte_count
        if len(assembly.chunks) != assembly.count:
            return None

        ordered = [assembly.chunks[index] for index in range(assembly.count)]
        first = ordered[0]
        running: list[str] = []
        pending: list[str] = []
        workflows: dict[str, Any] = {}
        workflow_truncated: list[str] = []
        omitted_nodes = 0
        for chunk in ordered:
            running.extend(chunk["running_prompt_ids"])
            pending.extend(chunk["pending_prompt_ids"])
            # First occurrence wins if a malformed upstream queue repeats one ID
            # with differing metadata. Queue membership remains complete.
            for prompt_id, workflow in chunk["workflows"].items():
                workflows.setdefault(prompt_id, workflow)
            for prompt_id in chunk.get("workflow_truncated_prompt_ids", []):
                if prompt_id not in workflow_truncated:
                    workflow_truncated.append(prompt_id)
            omitted_nodes += chunk["truncated"]["workflow_nodes"]

        complete = {
            "kind": "snapshot",
            "schema": SCHEMA_VERSION,
            "endpoint": first["endpoint"],
            "instance_id": first["instance_id"],
            "observed_at": first["observed_at"],
            "online": True,
            "running_prompt_ids": running,
            "pending_prompt_ids": pending,
            "workflows": workflows,
            "workflow_truncated_prompt_ids": workflow_truncated,
            "truncated": {
                "running_prompt_ids": 0,
                "pending_prompt_ids": 0,
                "workflow_nodes": omitted_nodes,
            },
        }
        self._settle_snapshot(snapshot_id, now)
        return complete if _valid_record(complete) else None

    def _consume(self, stream: Any) -> None:
        compatible = False
        while not self._stop.is_set():
            line = stream.readline(MAX_NDJSON_BYTES + 1)
            if line == "":
                return
            if self._stop.is_set():
                return
            if not isinstance(line, str):
                continue
            if len(line.encode("utf-8", errors="replace")) > MAX_NDJSON_BYTES:
                # Discard the rest of an overlong physical line in bounded reads.
                while line and not line.endswith("\n"):
                    line = stream.readline(MAX_NDJSON_BYTES + 1)
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(record, dict) or not _valid_record(record):
                continue
            if record.get("kind") == "hello":
                compatible = record.get("version") == PROTOCOL_VERSION
                self._assemblies.clear()
                self._settled_snapshots.clear()
                if not compatible:
                    self.on_error("incompatible probe protocol")
                continue
            if compatible:
                # Online queue state is authoritative only after chunk assembly.
                # The only ordinary snapshot accepted directly from the probe is
                # the explicitly empty offline record.
                if record["kind"] == "snapshot" and record["online"]:
                    continue
                if record["kind"] == "snapshot_chunk":
                    record = self._assemble_snapshot_chunk(
                        record, byte_count=len(line.encode("utf-8"))
                    )
                    if record is None:
                        continue
                self.on_record(record)

    def _launch_once(self) -> None:
        process = self._popen(
            list(self.argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=False,
        )
        with self._lock:
            self._process = process
        stderr_thread: threading.Thread | None = None
        try:
            stderr_thread = threading.Thread(
                target=self._report_stderr, args=(process.stderr,), daemon=True
            )
            stderr_thread.start()
            self._consume(process.stdout)
        finally:
            # Every launch owns its child until complete cleanup. This also covers
            # EOF while a child lives and exceptions from streams or callbacks.
            try:
                self._stop_process(process)
            finally:
                if stderr_thread is not None and stderr_thread.ident is not None:
                    stderr_thread.join()
                with self._lock:
                    if self._process is process:
                        self._process = None

    def _supervise(self) -> None:
        failure = 0
        while not self._stop.is_set():
            try:
                self._launch_once()
            except Exception as exc:
                self.on_error(_redact(str(exc)))
            if self._stop.is_set():
                break
            delay = self._backoff[min(failure, len(self._backoff) - 1)]
            failure += 1
            self._stop.wait(delay)


class SSHSource(LocalSource):
    """A restartable source backed by one persistent batch-mode SSH process."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        remote_argv: Sequence[str],
        on_record: Callable[[dict[str, Any]], None],
        port: int = 22,
        identity_file: str | None = None,
        on_error: Callable[[str], None] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        backoff: Sequence[float] = (0.25, 1.0, 2.0, 5.0),
    ) -> None:
        super().__init__(
            build_ssh_argv(
                host=host,
                user=user,
                remote_argv=remote_argv,
                port=port,
                identity_file=identity_file,
            ),
            on_record=on_record,
            on_error=on_error,
            popen=popen,
            backoff=backoff,
        )
