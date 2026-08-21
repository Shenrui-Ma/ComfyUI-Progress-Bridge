import json
import os
import socket
import threading
import time
from dataclasses import replace
from uuid import UUID

import pytest

from comfyui_progress_bridge.desktop import notifications as notifications_module
from comfyui_progress_bridge.desktop.notifications import (
    MAX_RESPONSE_BYTES,
    CompletionDispatcher,
    FixedOriginHttpsTransport,
    HttpResponse,
    NotificationSender,
    _DaemonResolver,
    _is_weixin_stale_session,
)
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    EndpointConfig,
    NotificationConfig,
    WeixinNotificationConfig,
)
from comfyui_progress_bridge.monitor.models import (
    EndpointId,
    EndpointState,
    MonitorState,
    Reduction,
    Transition,
)


class PlainTlsContext:
    def __init__(self):
        self.server_names = []

    def wrap_socket(self, sock, *, server_hostname):
        self.server_names.append(server_hostname)
        return sock


def _serve(parts, delays=()):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted = []

    def run():
        connection, _ = listener.accept()
        accepted.append(True)
        try:
            connection.recv(65536)
            for index, part in enumerate(parts):
                connection.sendall(part)
                if index < len(delays):
                    time.sleep(delays[index])
        except OSError:
            pass
        finally:
            connection.close()
            listener.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return listener.getsockname()[1], accepted, worker


def _transport_for(port):
    def resolver(_host, _port, _timeout):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    context = PlainTlsContext()
    return FixedOriginHttpsTransport(
        resolver=resolver, ssl_context=context, proxy_url=""
    ), context


def test_fixed_origin_transport_parses_chunked_and_preserves_sni():
    port, _, worker = _serve(
        [
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            b'4\r\n{"ok\r\n',
            b'4\r\n":1}\r\n0\r\n\r\n',
        ]
    )
    transport, context = _transport_for(port)
    result = transport.request(
        "POST",
        "https://api.telegram.org/test",
        headers={"Authorization": "secret"},
        json_body={},
        timeout=1,
        max_response_bytes=100,
    )
    worker.join(1)
    assert result == HttpResponse(200, b'{"ok":1}')
    assert context.server_names == ["api.telegram.org"]


def test_fixed_origin_transport_rejects_redirect_without_credential_forwarding():
    attacker = socket.socket()
    attacker.bind(("127.0.0.1", 0))
    attacker.listen()
    attacker.settimeout(0.15)
    attacker_port = attacker.getsockname()[1]
    port, accepted, worker = _serve(
        [
            (
                f"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1:{attacker_port}/steal\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode()
        ]
    )
    transport, _ = _transport_for(port)
    with pytest.raises(ValueError, match="redirect"):
        transport.request(
            "POST",
            "https://api.telegram.org/botSECRET/sendMessage",
            headers={"Authorization": "Bearer SECRET"},
            json_body={},
            timeout=0.5,
            max_response_bytes=100,
        )
    worker.join(1)
    assert accepted == [True]
    with pytest.raises(TimeoutError):
        attacker.accept()
    attacker.close()


@pytest.mark.parametrize(
    "parts",
    [
        [b"H", b"T", b"T", b"P", b"/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"],
        [b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n", b"a", b"b", b"c", b"d", b"e"],
    ],
)
def test_transport_total_deadline_defeats_header_and_body_drip(parts):
    port, _, worker = _serve(parts, [0.035] * len(parts))
    transport, _ = _transport_for(port)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        transport.request(
            "POST",
            "https://api.telegram.org/test",
            headers={},
            json_body={},
            timeout=0.09,
            max_response_bytes=100,
        )
    assert time.monotonic() - started < 0.3
    worker.join(1)


def _serve_sequence(responses):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def run():
        try:
            for delay, body in responses:
                connection, _ = listener.accept()
                try:
                    connection.recv(65536)
                    time.sleep(delay)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
                except OSError:
                    pass
                finally:
                    connection.close()
        finally:
            listener.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return listener.getsockname()[1], worker


def test_fixed_origin_requests_share_one_absolute_deadline():
    body = b'{"ret":-2,"errcode":-2,"errmsg":"unknown error"}'
    port, worker = _serve_sequence([(0.1, body), (0.2, b'{"ret":0}')])
    transport, _ = _transport_for(port)
    deadline = transport.monotonic() + 0.25
    started = time.monotonic()

    first = transport.request(
        "POST",
        "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
        headers={},
        json_body={},
        timeout=0.25,
        deadline=deadline,
        max_response_bytes=100,
    )
    assert first.body == body
    with pytest.raises(TimeoutError):
        transport.request(
            "POST",
            "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
            headers={},
            json_body={},
            timeout=0.11,
            deadline=deadline,
            max_response_bytes=100,
        )
    assert time.monotonic() - started < 0.4
    worker.join(1)


def test_daemon_resolver_is_deadline_bounded_and_single_worker(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: gate.wait(1) or [])
    resolver = _DaemonResolver()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        resolver("api.telegram.org", 443, 0.03)
    assert time.monotonic() - started < 0.2
    assert resolver._thread.daemon
    gate.set()


def _wx_settings(tmp_path, account, store, *, timeout: float = 10):
    env = tmp_path / "credentials.env"
    env.write_text("WEIXIN_TOKEN=token\n")
    env.chmod(0o600)
    return AppSettings(
        notifications=NotificationConfig(
            enabled=True,
            env_file=str(env),
            timeout=timeout,
            weixin=WeixinNotificationConfig(True, account, "peer", str(store)),
        )
    )


class HostileErrmsg(str):
    def __new__(cls):
        return super().__new__(cls, "unknown error")

    def casefold(self):
        raise RuntimeError("must not execute attacker methods")

    def lower(self):
        raise RuntimeError("must not execute attacker methods")


class HostileInt(int):
    def __eq__(self, _other):
        raise RuntimeError("must not execute attacker methods")


@pytest.mark.parametrize(
    "payload",
    [
        {"ret": -2.0, "errcode": -2, "errmsg": "unknown error"},
        {"ret": -2, "errcode": -2.0, "errmsg": "unknown error"},
        {"ret": "-2", "errcode": -2, "errmsg": "unknown error"},
        {"ret": -2, "errcode": "-2", "errmsg": "unknown error"},
        {"ret": True, "errcode": -2, "errmsg": "unknown error"},
        {"ret": -2, "errcode": True, "errmsg": "unknown error"},
        {"ret": -2, "errmsg": "unknown error"},
        {"errcode": -2, "errmsg": "unknown error"},
        {"ret": -2, "errcode": 0, "errmsg": "unknown error"},
        {"ret": 0, "errcode": -2, "errmsg": "unknown error"},
        {"ret": -2, "errcode": -2, "errmsg": None},
        {"ret": -2, "errcode": -2, "errmsg": ["unknown error"]},
        {"ret": -2, "errcode": -2, "errmsg": HostileErrmsg()},
        {"ret": -14.0},
        {"errcode": "-14"},
        {"ret": False, "errcode": -14},
        {"ret": -14, "errcode": 9},
    ],
)
def test_weixin_stale_session_classifier_fails_closed(payload):
    assert not _is_weixin_stale_session(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"ret": -14},
        {"errcode": -14},
        {"ret": -2, "errcode": -2, "errmsg": "Unknown Error"},
    ],
)
def test_weixin_documented_stale_shapes_are_strictly_recognized(payload):
    assert _is_weixin_stale_session(payload)


_INVALID_WEIXIN_SUCCESS_PAYLOADS = [
    {},
    {"errcode": 0},
    {"ret": None},
    {"ret": False},
    {"ret": 0.0},
    {"ret": "0"},
    {"ret": 1, "errcode": 0},
    {"ret": 0, "errcode": None},
    {"ret": 0, "errcode": False},
    {"ret": 0, "errcode": 0.0},
    {"ret": 0, "errcode": "0"},
    {"ret": 0, "errcode": 1},
]


@pytest.mark.parametrize(
    "payload",
    _INVALID_WEIXIN_SUCCESS_PAYLOADS,
    ids=[
        "empty",
        "missing-ret",
        "null-ret",
        "bool-ret",
        "float-ret",
        "string-ret",
        "nonzero-ret",
        "null-errcode",
        "bool-errcode",
        "float-errcode",
        "string-errcode",
        "conflicting-errcode",
    ],
)
def test_weixin_success_validator_rejects_malformed_shapes(payload):
    assert not notifications_module._is_weixin_success(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"ret": HostileInt(0)},
        {"ret": 0, "errcode": HostileInt(0)},
    ],
)
def test_weixin_success_validator_rejects_int_subclasses_without_comparison(payload):
    assert not notifications_module._is_weixin_success(payload)


@pytest.mark.parametrize("payload", [{"ret": 0}, {"ret": 0, "errcode": 0}])
def test_weixin_success_validator_accepts_documented_shapes(payload):
    assert notifications_module._is_weixin_success(payload)


@pytest.mark.parametrize("payload", _INVALID_WEIXIN_SUCCESS_PAYLOADS)
def test_weixin_malformed_first_response_is_api_error_without_retry_or_leak(tmp_path, payload):
    secret = "hostile-response-secret"
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "context"}))
    settings = _wx_settings(tmp_path, "acct", context)
    transport = type(
        "Transport",
        (),
        {
            "calls": 0,
            "request": lambda self, *_a, **_kw: _count_response(
                self, HttpResponse(200, json.dumps({**payload, "detail": secret}).encode())
            ),
        },
    )()

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result.code == "api_error"
    assert transport.calls == 1
    assert secret not in repr(result) and secret not in result.message


@pytest.mark.parametrize("payload", _INVALID_WEIXIN_SUCCESS_PAYLOADS)
def test_weixin_malformed_tokenless_retry_is_api_error_without_third_send_or_leak(
    tmp_path, payload
):
    secret = "hostile-retry-response-secret"
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "context"}))
    settings = _wx_settings(tmp_path, "acct", context)

    class Transport:
        def __init__(self):
            self.calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls > 2:
                pytest.fail("a malformed tokenless response must not cause a third send")
            body = {"ret": -14} if self.calls == 1 else {**payload, "detail": secret}
            return HttpResponse(200, json.dumps(body).encode())

    transport = Transport()
    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result.code == "api_error"
    assert transport.calls == 2
    assert secret not in repr(result) and secret not in result.message


def _count_response(transport, result):
    transport.calls += 1
    return result


def test_weixin_malformed_or_oversized_first_response_never_retries(tmp_path):
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "context"}))
    settings = _wx_settings(tmp_path, "acct", context)

    for first in (
        HttpResponse(200, b"not-json"),
        HttpResponse(200, b"{" + b" " * MAX_RESPONSE_BYTES + b"}"),
    ):
        transport = type(
            "Transport",
            (),
            {
                "calls": 0,
                "request": lambda self, *_a, _first=first, **_kw: _count_response(
                    self, _first
                ),
            },
        )()
        result = NotificationSender(transport).send_platform("weixin", "done", settings)
        assert result.code == "api_error"
        assert transport.calls == 1


def test_weixin_stale_tokenless_response_does_not_trigger_third_send(tmp_path):
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "context"}))
    settings = _wx_settings(tmp_path, "acct", context)

    class Transport:
        calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls > 2:
                pytest.fail("a stale tokenless response must not cause a third send")
            body = {"ret": -2, "errcode": -2, "errmsg": "unknown error"}
            return HttpResponse(200, json.dumps(body).encode())

    transport = Transport()
    result = NotificationSender(transport).send_platform("weixin", "done", settings)
    assert result.code == "api_error"
    assert transport.calls == 2


def test_weixin_sender_and_fixed_transport_use_one_monotonic_deadline(tmp_path):
    stale = b'{"ret":-2,"errcode":-2,"errmsg":"unknown error"}'
    port, worker = _serve_sequence([(0.65, stale), (0.65, b'{"ret":0}')])
    transport, _ = _transport_for(port)
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "context"}))
    settings = _wx_settings(tmp_path, "acct", context, timeout=1)
    started = time.monotonic()

    # The deliberately unrelated injected clock used to let each transport
    # request receive a fresh full timeout.
    result = NotificationSender(transport, monotonic=lambda: 0.0).send_platform(
        "weixin", "done", settings
    )

    assert result.code == "timeout"
    assert time.monotonic() - started < 1.2
    worker.join(1)


def test_weixin_account_traversal_is_rejected_before_context_read(tmp_path):
    transport = type("Transport", (), {"request": lambda *_a, **_kw: pytest.fail("sent")})()
    result = NotificationSender(transport).send_platform(
        "weixin", "done", _wx_settings(tmp_path, "../../escape", tmp_path)
    )
    assert result.code == "invalid_settings"


def test_weixin_context_symlink_and_lstat_open_swap_fail_closed(tmp_path, monkeypatch):
    context = tmp_path / "acct.context-tokens.json"
    context.write_text(json.dumps({"peer": "good"}))
    secret = tmp_path / "secret"
    secret.write_text(json.dumps({"peer": "stolen"}))
    settings = _wx_settings(tmp_path, "acct", context)
    assert NotificationSender._context_token(settings.notifications, "acct", "peer") == "good"
    context.unlink()
    context.symlink_to(secret)
    assert NotificationSender._context_token(settings.notifications, "acct", "peer") == ""

    context.unlink()
    context.write_text(json.dumps({"peer": "good"}))
    real_open = os.open

    def swapping_open(path, flags, *args):
        if os.fspath(path) == os.fspath(context):
            context.unlink()
            context.symlink_to(secret)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", swapping_open)
    assert NotificationSender._context_token(settings.notifications, "acct", "peer") == ""


def _complete(endpoint, epoch, *, transition=True):
    state = EndpointState(endpoint, online=True, busy=False, busy_epoch=epoch)
    return Reduction(
        MonitorState.from_parts({endpoint: state}, {}, {}),
        (Transition("queue_completed", endpoint, busy_epoch=epoch),) if transition else (),
    )


class GatedSender:
    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def send_enabled(self, _text, _settings):
        self.calls += 1
        self.started.set()
        self.gate.wait(1)
        return ()


def test_dispatcher_never_notifies_epoch_zero_or_running_state():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    dispatcher = CompletionDispatcher(GatedSender(), AppSettings())
    initial = Reduction(
        MonitorState.from_parts(
            {endpoint: EndpointState(endpoint, online=True, busy=False, busy_epoch=0)}, {}, {}
        ),
        (Transition("queue_completed", endpoint, busy_epoch=1),),
    )
    running = Reduction(
        MonitorState.from_parts(
            {endpoint: EndpointState(endpoint, online=True, busy=True, busy_epoch=1)}, {}, {}
        ),
        (),
    )
    assert dispatcher.dispatch(initial, {}) == 0
    assert dispatcher.dispatch(running, {}) == 0
    assert dispatcher.shutdown(1)


def test_dispatcher_full_queue_epoch_can_retry():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = GatedSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=1)
    assert dispatcher.dispatch(_complete(endpoint, 1), {}) == 1
    assert sender.started.wait(1)
    assert dispatcher.dispatch(_complete(endpoint, 2), {}) == 1
    assert dispatcher.dispatch(_complete(endpoint, 3), {}) == 0
    sender.gate.set()
    deadline = time.monotonic() + 1
    while dispatcher._queue.full() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert dispatcher.dispatch(_complete(endpoint, 3), {}) == 1
    assert dispatcher.shutdown(1)
    assert sender.calls == 3


def test_dispatcher_fresh_idle_epoch_without_transition_does_not_notify():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = GatedSender()
    dispatcher = CompletionDispatcher(sender, AppSettings())

    assert dispatcher.dispatch(_complete(endpoint, 7, transition=False), {}) == 0
    assert dispatcher._pending_retry == {}
    assert dispatcher.shutdown(1)
    assert sender.calls == 0


def test_dispatcher_full_queue_retries_from_ordinary_idle_reduction_once():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = GatedSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=1)
    assert dispatcher.dispatch(_complete(endpoint, 1), {}) == 1
    assert sender.started.wait(1)
    assert dispatcher.dispatch(_complete(endpoint, 2), {}) == 1
    assert dispatcher.dispatch(_complete(endpoint, 3), {}) == 0
    assert dispatcher._pending_retry[(endpoint.host, endpoint.port)].busy_epoch == 3

    sender.gate.set()
    deadline = time.monotonic() + 1
    while dispatcher._queue.full() and time.monotonic() < deadline:
        time.sleep(0.005)
    ordinary_idle = _complete(endpoint, 3, transition=False)
    assert dispatcher.dispatch(ordinary_idle, {}) == 1
    assert dispatcher.dispatch(ordinary_idle, {}) == 0
    assert dispatcher.shutdown(1)
    assert sender.calls == 3


def test_dispatcher_remove_and_readd_drops_backpressure_retry():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = GatedSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=1)
    assert dispatcher.dispatch(_complete(endpoint, 1), {}) == 1
    assert sender.started.wait(1)
    assert dispatcher.dispatch(_complete(endpoint, 2), {}) == 1
    assert dispatcher.dispatch(_complete(endpoint, 3), {}) == 0

    dispatcher.update_settings(
        replace(AppSettings(), endpoints=(EndpointConfig("127.0.0.2", 8189, "Other"),))
    )
    assert dispatcher._pending_retry == {}
    dispatcher.update_settings(AppSettings())
    sender.gate.set()
    deadline = time.monotonic() + 1
    while dispatcher._queue.full() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert dispatcher.dispatch(_complete(endpoint, 3, transition=False), {}) == 0
    assert dispatcher.shutdown(1)
    assert sender.calls == 2


def test_dispatcher_concurrent_duplicate_is_enqueued_once():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = GatedSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=8)
    reduction = _complete(endpoint, 1)
    barrier = threading.Barrier(8)
    results = []

    def dispatch():
        barrier.wait()
        results.append(dispatcher.dispatch(reduction, {}))

    workers = [threading.Thread(target=dispatch) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    sender.gate.set()
    assert dispatcher.shutdown(1)
    assert sum(results) == 1
    assert sender.calls == 1


def test_dispatcher_worker_survives_unexpected_sender_exception():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))

    class Flaky:
        def __init__(self):
            self.calls = 0

        def send_enabled(self, _text, _settings):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("SECRET")
            return ()

    sender = Flaky()
    dispatcher = CompletionDispatcher(sender, AppSettings())
    assert dispatcher.dispatch(_complete(endpoint, 1), {}) == 1
    deadline = time.monotonic() + 1
    while not dispatcher.last_results and time.monotonic() < deadline:
        time.sleep(0.005)
    assert dispatcher.last_results[0].code == "network_error"
    assert "SECRET" not in repr(dispatcher.last_results)
    assert dispatcher.dispatch(_complete(endpoint, 2), {}) == 1
    assert dispatcher.shutdown(1)
    assert sender.calls == 2
