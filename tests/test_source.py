import io
import json
import sys
import time

import pytest

from comfyui_progress_bridge.monitor.source import (
    PROTOCOL_VERSION,
    LocalSource,
    SSHSource,
    _valid_record,
    build_local_argv,
    build_ssh_argv,
)


class FakeProcess:
    def __init__(self, stdout, stderr="", returncode=0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode if self.terminated else None

    def wait(self, timeout=None):
        self.terminated = True
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True


def hello(version=PROTOCOL_VERSION):
    return json.dumps({"kind": "hello", "schema": 2, "version": version}) + "\n"


def snapshot(**updates):
    record = {
        "kind": "snapshot",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": "00000000-0000-0000-0000-000000000001",
        "observed_at": 1.0,
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
    record.update(updates)
    return record


def snapshot_chunk(index, count, prompt_id, *, state="running", snapshot_id=None, **updates):
    record = snapshot(
        kind="snapshot_chunk",
        online=True,
        snapshot_id=snapshot_id or "10000000-0000-0000-0000-000000000001",
        chunk_index=index,
        chunk_count=count,
        running_prompt_ids=[prompt_id] if state == "running" else [],
        pending_prompt_ids=[prompt_id] if state == "pending" else [],
    )
    record.update(updates)
    return record


def event(**updates):
    record = {
        "kind": "event",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": "00000000-0000-0000-0000-000000000001",
        "sequence": 0,
        "observed_at": 1.0,
        "type": "progress",
        "data": {"prompt_id": "task", "value": 1, "max": 2},
    }
    record.update(updates)
    return record


def status(**updates):
    record = {
        "kind": "status",
        "schema": 2,
        "code": "record_too_large",
        "record_kind": "snapshot",
        "endpoint": None,
        "instance_id": None,
    }
    record.update(updates)
    return record


def test_local_source_uses_argv_no_shell_and_ignores_malformed_or_oversized_ndjson():
    launches = []
    records = []
    payload = hello() + "bad json\n" + ("x" * 70000) + "\n" + json.dumps(snapshot()) + "\n"

    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return FakeProcess(payload)

    source = LocalSource(
        ["python3", "-m", "probe"],
        on_record=records.append,
        popen=popen,
        backoff=(0,),
    )
    source.start()
    deadline = time.monotonic() + 1
    while not records and time.monotonic() < deadline:
        time.sleep(0.001)
    source.stop()
    source.join(timeout=1)

    assert records
    assert all(record == snapshot() for record in records)
    assert launches[0][0] == ["python3", "-m", "probe"]
    assert launches[0][1]["shell"] is False


def test_source_restarts_after_exit_and_clean_stop_joins_and_terminates_child():
    launches = []

    def popen(argv, **kwargs):
        process = FakeProcess(hello())
        launches.append(process)
        return process

    source = LocalSource(["probe"], on_record=lambda record: None, popen=popen, backoff=(0,))
    source.start()
    deadline = time.monotonic() + 1
    while len(launches) < 2 and time.monotonic() < deadline:
        # End each launched fake so the supervisor restarts it.
        launches[-1].terminate()
        time.sleep(0.001)
    source.stop()
    source.join(timeout=1)

    assert len(launches) >= 2
    assert not source.is_alive()
    assert launches[-1].terminated


def test_version_handshake_rejects_records_from_incompatible_probe():
    records = []
    errors = []

    def popen(argv, **kwargs):
        return FakeProcess(hello("99") + '{"kind":"snapshot","schema":2}\n')

    source = LocalSource(
        ["probe"],
        on_record=records.append,
        on_error=errors.append,
        popen=popen,
        backoff=(1,),
    )
    source.start()
    deadline = time.monotonic() + 1
    while not errors and time.monotonic() < deadline:
        time.sleep(0.001)
    source.stop()
    source.join(timeout=1)
    assert records == []
    assert errors == ["incompatible probe protocol"]


def test_stderr_is_captured_bounded_and_credentials_are_redacted():
    errors = []

    def popen(argv, **kwargs):
        return FakeProcess(hello(), "failed token=supersecret password=hunter2\n")

    source = LocalSource(
        ["probe"], on_record=lambda record: None, on_error=errors.append, popen=popen, backoff=(1,)
    )
    source.start()
    deadline = time.monotonic() + 1
    while not errors and time.monotonic() < deadline:
        time.sleep(0.001)
    source.stop()
    source.join(timeout=1)
    combined = " ".join(errors)
    assert "supersecret" not in combined
    assert "hunter2" not in combined
    assert "[REDACTED]" in combined


def test_command_builders_validate_and_ssh_is_persistent_argv_without_password():
    assert build_local_argv(["python3", "-u", "probe.py"]) == ["python3", "-u", "probe.py"]
    argv = build_ssh_argv(
        host="worker.example",
        user="monitor",
        remote_argv=["python3", "-u", "/opt/probe.py", "8189"],
        identity_file="/keys/monitor",
    )
    assert argv == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        "/keys/monitor",
        "monitor@worker.example",
        "python3",
        "-u",
        "/opt/probe.py",
        "8189",
    ]
    source = SSHSource(
        host="worker.example",
        user="monitor",
        remote_argv=["probe"],
        on_record=lambda record: None,
        popen=lambda *args, **kwargs: FakeProcess(""),
    )
    assert source.argv[-2:] == ["monitor@worker.example", "probe"]
    assert build_ssh_argv(
        host="worker.example", user="monitor", port=2222, remote_argv=["probe"]
    )[-4:] == ["-p", "2222", "monitor@worker.example", "probe"]


def test_strict_record_validation_drops_malformed_required_fields_and_uuid_types():
    valid_event = {
        "kind": "event",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": "00000000-0000-0000-0000-000000000001",
        "sequence": 0,
        "observed_at": 1.0,
        "type": "progress",
        "data": {"prompt_id": "task", "value": 1, "max": 2},
    }
    malformed = [
        {"kind": "hello", "schema": 2},
        snapshot(instance_id=[]),
        snapshot(online=0),
        snapshot(running_prompt_ids="not-a-list"),
        {**valid_event, "sequence": True},
        {**valid_event, "observed_at": float("inf")},
        {**valid_event, "data": []},
    ]
    records = []
    source = LocalSource(["probe"], on_record=records.append)
    payload = hello() + "".join(json.dumps(item) + "\n" for item in malformed)
    payload += json.dumps(snapshot()) + "\n" + json.dumps(valid_event) + "\n"
    source._consume(io.StringIO(payload))
    assert records == [snapshot(), valid_event]


def test_every_protocol_kind_and_known_status_reject_unknown_top_level_fields():
    records = [
        {"kind": "hello", "schema": 2, "version": PROTOCOL_VERSION},
        snapshot(),
        snapshot_chunk(0, 1, "task"),
        event(),
        status(),
    ]

    assert all(_valid_record(record) for record in records)
    assert all(not _valid_record({**record, "unexpected": True}) for record in records)
    assert not _valid_record(status(code="unknown_status"))


def test_snapshot_chunks_are_assembled_out_of_order_once_without_losing_tasks():
    records = []
    source = LocalSource(["probe"], on_record=records.append)
    first = snapshot_chunk(0, 2, "running-task")
    second = snapshot_chunk(1, 2, "pending-task", state="pending")
    payload = hello() + "".join(
        json.dumps(item) + "\n" for item in (second, second, first, first)
    )

    source._consume(io.StringIO(payload))

    assert len(records) == 1
    assert records[0]["kind"] == "snapshot"
    assert records[0]["running_prompt_ids"] == ["running-task"]
    assert records[0]["pending_prompt_ids"] == ["pending-task"]
    assert "snapshot_id" not in records[0]
    assert source._assemblies == {}


def test_missing_or_conflicting_snapshot_chunks_never_become_authoritative():
    records = []
    source = LocalSource(["probe"], on_record=records.append)
    incomplete = snapshot_chunk(0, 2, "only-task")
    conflicting = snapshot_chunk(0, 2, "different-task")
    final = snapshot_chunk(1, 2, "late-task")

    source._consume(
        io.StringIO(
            hello()
            + "".join(json.dumps(item) + "\n" for item in (incomplete, conflicting, final))
        )
    )

    assert records == []
    assert source._assemblies == {}


def test_semantically_invalid_protocol_records_are_rejected():
    base_event = {
        "kind": "event",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": "00000000-0000-0000-0000-000000000001",
        "sequence": 1,
        "observed_at": 1.0,
        "type": "progress",
        "data": {"prompt_id": "task", "value": 1, "max": 2},
    }
    invalid = [
        {**base_event, "type": "made_up"},
        {**base_event, "data": {"value": 1, "max": 2}},
        {**base_event, "data": {"prompt_id": "task", "value": "1", "max": 2}},
        {**base_event, "endpoint": {"host": "example.com", "port": 8188}},
        snapshot(online=False, running_prompt_ids=["must-not-be-active"]),
        snapshot(online=True),
        snapshot(workflows={"absent": {"1": {"node_type": "KSampler"}}}),
        snapshot(
            truncated={
                "running_prompt_ids": -1,
                "pending_prompt_ids": 0,
                "workflow_nodes": 0,
            }
        ),
        snapshot_chunk(2, 2, "out-of-range"),
    ]
    records = []
    source = LocalSource(["probe"], on_record=records.append)

    source._consume(io.StringIO(hello() + "".join(json.dumps(item) + "\n" for item in invalid)))

    assert records == []


def test_stop_kills_sigterm_ignoring_child_with_open_stdout_and_joins_bounded():
    script = (
        "import json,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print(json.dumps({'kind':'hello','schema':2,'version':'1'}),flush=True);"
        "time.sleep(60)"
    )
    source = LocalSource([sys.executable, "-c", script], on_record=lambda _record: None)
    source.start()
    deadline = time.monotonic() + 2
    while source._process is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert source._process is not None
    process = source._process
    time.sleep(0.1)  # Allow the child to install its SIGTERM handler.
    assert process.poll() is None
    started = time.monotonic()
    source.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert process.returncode != 0
    assert not source.is_alive()


@pytest.mark.parametrize(
    "host,user",
    [
        ("-oProxyCommand=bad", "monitor"),
        ("worker;bad", "monitor"),
        ("worker.example", "-root"),
        ("worker.example", "user@evil"),
    ],
)
def test_ssh_destination_rejects_leading_options_and_unsafe_characters(host, user):
    with pytest.raises(ValueError):
        build_ssh_argv(host=host, user=user, remote_argv=["probe"])


@pytest.mark.parametrize("backoff", [(True,), (float("inf"),), (float("nan"),), (-1,)])
def test_source_rejects_invalid_backoff_values(backoff):
    with pytest.raises(ValueError, match="backoff"):
        LocalSource(["probe"], on_record=lambda _record: None, backoff=backoff)


@pytest.mark.parametrize("timeout", [True, float("inf"), float("nan"), -1])
def test_source_rejects_invalid_join_timeouts(timeout):
    source = LocalSource(["probe"], on_record=lambda _record: None)
    with pytest.raises(ValueError, match="join timeout"):
        source.join(timeout)


def test_callback_exception_still_terminates_reaps_closes_and_joins_before_restart():
    processes = []

    def popen(*_args, **_kwargs):
        process = FakeProcess(hello() + json.dumps(event()) + "\n", "diagnostic")
        processes.append(process)
        return process

    def raising_callback(_record):
        raise RuntimeError("callback failed")

    source = LocalSource(
        ["probe"],
        on_record=raising_callback,
        on_error=lambda _message: None,
        popen=popen,
        backoff=(0,),
    )
    source.start()
    deadline = time.monotonic() + 1
    while len(processes) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    source.stop()

    assert len(processes) >= 2
    assert all(process.terminated for process in processes)
    assert all(process.stdout.closed and process.stderr.closed for process in processes)
    assert not source.is_alive()


def test_normal_eof_terminates_live_child_and_closes_both_streams_before_restart():
    processes = []

    def popen(*_args, **_kwargs):
        if processes:
            assert processes[-1].terminated
            assert processes[-1].stdout.closed
            assert processes[-1].stderr.closed
        process = FakeProcess(hello())
        processes.append(process)
        return process

    source = LocalSource(["probe"], on_record=lambda _record: None, popen=popen, backoff=(0,))
    source.start()
    deadline = time.monotonic() + 1
    while len(processes) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    source.stop()

    assert len(processes) >= 2
    assert all(process.terminated for process in processes)
    assert all(process.stdout.closed and process.stderr.closed for process in processes)
