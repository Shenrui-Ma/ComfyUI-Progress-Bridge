import base64
import json
import socket
import ssl
import threading
import time

import pytest

from comfyui_progress_bridge.desktop.notifications import (
    FixedOriginHttpsTransport,
    HttpResponse,
    NotificationSender,
)
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    NotificationConfig,
    WeixinNotificationConfig,
)


class PlainTlsContext:
    def __init__(self):
        self.server_names = []

    def wrap_socket(self, sock, *, server_hostname):
        self.server_names.append(server_hostname)
        return sock


def serve_proxy(connect_reply=b"HTTP/1.1 200 Connection Established\r\n\r\n", *, drip=0):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    captured = []

    def run():
        connection, _ = listener.accept()
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            captured.append(request)
            if drip:
                for byte in connect_reply:
                    connection.sendall(bytes((byte,)))
                    time.sleep(drip)
            else:
                connection.sendall(connect_reply)
            status = int(connect_reply.split(b" ", 2)[1])
            if 200 <= status < 300:
                origin = b""
                while b"\r\n\r\n" not in origin:
                    origin += connection.recv(4096)
                captured.append(origin)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            pass
        finally:
            connection.close()
            listener.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return listener.getsockname()[1], captured, worker


def proxy_transport(port, proxy_url=None):
    def resolver(host, _port, _timeout):
        assert host == "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    context = PlainTlsContext()
    return FixedOriginHttpsTransport(
        resolver=resolver,
        ssl_context=context,
        proxy_url=proxy_url or f"http://127.0.0.1:{port}",
    ), context


def request(transport, timeout=1):
    return transport.request(
        "POST",
        "https://api.telegram.org/test",
        headers={"Authorization": "Bearer origin-secret"},
        json_body={},
        timeout=timeout,
        max_response_bytes=100,
    )


def test_https_proxy_connect_is_exact_and_auth_is_proxy_only():
    port, captured, worker = serve_proxy()
    credentials = base64.b64encode(b"proxy-user:proxy-pass").decode("ascii")
    transport, context = proxy_transport(
        port, f"http://proxy-user:proxy-pass@127.0.0.1:{port}"
    )
    assert request(transport) == HttpResponse(200, b"{}")
    worker.join(1)
    assert captured[0] == (
        b"CONNECT api.telegram.org:443 HTTP/1.1\r\n"
        b"Host: api.telegram.org:443\r\n"
        + f"Proxy-Authorization: Basic {credentials}\r\n".encode()
        + b"\r\n"
    )
    assert b"Proxy-Authorization" not in captured[1]
    assert ("Authorization: Bearer origin-" + "secret\r\n").encode() in captured[1]
    assert context.server_names == ["api.telegram.org"]



def test_https_proxy_environment_default_honors_no_proxy(monkeypatch):
    port, captured, worker = serve_proxy()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "not-api.telegram.org")

    def resolver(host, _port, _timeout):
        assert host == "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    transport = FixedOriginHttpsTransport(resolver=resolver, ssl_context=PlainTlsContext())
    assert request(transport).status == 200
    worker.join(1)
    assert captured[0].startswith(b"CONNECT api.telegram.org:443 HTTP/1.1\r\n")

    monkeypatch.setenv("NO_PROXY", "api.telegram.org")

    def bypass_resolver(host, _port, _timeout):
        assert host == "api.telegram.org"
        raise OSError("direct resolution selected")

    bypassed = FixedOriginHttpsTransport(
        resolver=bypass_resolver, ssl_context=PlainTlsContext()
    )
    with pytest.raises(OSError, match="direct resolution selected"):
        request(bypassed)


def test_https_proxy_rejects_non_2xx_and_drip_exhausts_total_deadline():
    port, captured, worker = serve_proxy(
        b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
    )
    transport, _ = proxy_transport(port)
    with pytest.raises(OSError, match="proxy tunnel"):
        request(transport)
    worker.join(1)
    assert len(captured) == 1

    oversized = b"HTTP/1.1 200 OK\r\nX-Fill: " + b"x" * (64 * 1024) + b"\r\n\r\n"
    port, _, worker = serve_proxy(oversized)
    transport, _ = proxy_transport(port)
    with pytest.raises(OSError, match="proxy tunnel"):
        request(transport)
    worker.join(1)

    port, _, worker = serve_proxy(drip=0.02)
    transport, _ = proxy_transport(port)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        request(transport, timeout=0.08)
    assert time.monotonic() - started < 0.3
    worker.join(1)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "https://proxy.example:443",
        "http://",
        "http://proxy.example:0",
        "http://proxy.example:99999",
        "http://user%0dname:pass@proxy.example:8080",
        "http://proxy.example:8080/path",
    ],
)
def test_malformed_https_proxy_is_rejected_without_resolution(proxy_url):
    transport = FixedOriginHttpsTransport(
        proxy_url=proxy_url,
        resolver=lambda *_args: pytest.fail("resolved malformed proxy"),
        ssl_context=PlainTlsContext(),
    )
    with pytest.raises(ValueError, match="proxy"):
        request(transport)


def test_direct_transport_retries_complete_tcp_tls_attempt_per_address():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    def serve_two():
        first, _ = listener.accept()
        first.close()
        second, _ = listener.accept()
        second.recv(65536)
        second.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        second.close()
        listener.close()

    worker = threading.Thread(target=serve_two, daemon=True)
    worker.start()

    class FirstTlsFails(PlainTlsContext):
        def wrap_socket(self, sock, *, server_hostname):
            super().wrap_socket(sock, server_hostname=server_hostname)
            if len(self.server_names) == 1:
                raise ssl.SSLError("first TLS failed")
            return sock

    context = FirstTlsFails()
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))] * 2
    transport = FixedOriginHttpsTransport(
        resolver=lambda *_args: addresses, ssl_context=context, proxy_url=""
    )
    assert request(transport) == HttpResponse(200, b"{}")
    worker.join(1)
    assert context.server_names == ["api.telegram.org", "api.telegram.org"]


def wx_settings(tmp_path, account):
    env = tmp_path / "credentials.env"
    env.write_text("WEIXIN_TOKEN=token\n")
    env.chmod(0o600)
    return AppSettings(
        notifications=NotificationConfig(
            enabled=True,
            env_file=str(env),
            weixin=WeixinNotificationConfig(True, account, "peer", str(tmp_path)),
        )
    )


def test_weixin_real_account_id_with_at_sign_uses_safe_component(tmp_path):
    account = "bot.local-part@example.com"
    context = tmp_path / f"{account}.context-tokens.json"
    context.write_text(json.dumps({"peer": "persisted"}))
    settings = wx_settings(tmp_path, account)
    transport = type(
        "Transport",
        (),
        {"request": lambda *_a, **_kw: HttpResponse(200, b'{"ret":0}')},
    )()
    result = NotificationSender(transport).send_platform("weixin", "done", settings)
    assert result.ok
    assert NotificationSender._context_path(settings.notifications, account) == context


@pytest.mark.parametrize("account", [".", "..", "a/b", "a\\b", "x" * 129, "a\x00b"])
def test_weixin_account_component_grammar_rejects_traversal(account, tmp_path):
    result = NotificationSender(
        type("Transport", (), {"request": lambda *_a, **_kw: pytest.fail("sent")})()
    ).send_platform("weixin", "done", wx_settings(tmp_path, account))
    assert result.code == "invalid_settings"
