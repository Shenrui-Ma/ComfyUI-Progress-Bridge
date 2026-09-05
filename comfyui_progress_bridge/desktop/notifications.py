"""Secure, one-shot completion notifications for official messaging APIs."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import ssl
import stat
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..monitor.models import EndpointId, Reduction
from .settings import AppSettings, NotificationConfig, config_directory

MAX_RESPONSE_BYTES = 262_144
MAX_ENV_BYTES = 1_048_576
MAX_REQUEST_BYTES = 1_048_576
MAX_HTTP_HEADER_BYTES = 64 * 1024
WEIXIN_RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
WEIXIN_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate limited",
    "frequency limit",
    "too frequent",
    "too many requests",
)
_ALLOWED_ENV = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "WEIXIN_TOKEN",
        "WEIXIN_ACCOUNT_ID",
        "WEIXIN_HOME_CHANNEL",
        "QQ_APP_ID",
        "QQ_CLIENT_SECRET",
    }
)
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9._@-]{1,128}\Z")


@dataclass(frozen=True)
class SafeResult:
    """A deliberately secret-free result suitable for UI display and repr()."""

    ok: bool
    code: str
    message: str
    platform: str = ""


# Stable adapter contract. UI code translates codes rather than displaying
# the human-oriented, non-contractual adapter message.
RESULT_CODES = frozenset(
    {
        "sent",
        "played",
        "valid",
        "disabled",
        "invalid_platform",
        "missing_credentials",
        "missing_target",
        "missing_context_token",
        "auth_error",
        "api_error",
        "network_error",
        "timeout",
        "busy",
        "missing_file",
        "unsafe_file",
        "invalid_file",
        "invalid_size",
        "invalid_format",
        "unavailable",
        "playback_error",
        "invalid_settings",
    }
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class _ProxyConfig:
    host: str
    port: int
    authorization: str | None


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class CompletionSender(Protocol):
    def send_enabled(self, text: str, settings: AppSettings) -> tuple[SafeResult, ...]: ...


class _LimitedHeaderFile:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._count = 0
        self._headers = True

    def finish_headers(self) -> None:
        self._headers = False

    def _account(self, data: bytes) -> bytes:
        if self._headers:
            self._count += len(data)
            if self._count > MAX_HTTP_HEADER_BYTES:
                raise ValueError("response headers are too large")
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
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.file: _LimitedHeaderFile | None = None

    def makefile(self, _mode: str, _buffering: int | None = None) -> _LimitedHeaderFile:
        self.file = _LimitedHeaderFile(self.sock.makefile("rb", buffering=0))
        return self.file


class _DaemonResolver:
    """A bounded resolver: libc DNS can stall, but never creates unbounded threads."""

    def __init__(self) -> None:
        self._requests: queue.Queue[tuple[str, int, threading.Event, list[Any]]] = queue.Queue(1)
        self._thread = threading.Thread(target=self._run, name="notification-dns", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            host, port, ready, result = self._requests.get()
            try:
                result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except BaseException as exc:
                result.append(exc)
            finally:
                ready.set()
                self._requests.task_done()

    def __call__(self, host: str, port: int, timeout: float) -> list[Any]:
        ready = threading.Event()
        result: list[Any] = []
        try:
            self._requests.put_nowait((host, port, ready, result))
        except queue.Full as exc:
            raise TimeoutError("DNS resolver is unavailable") from exc
        if not ready.wait(max(0.0, timeout)):
            raise TimeoutError("DNS resolution deadline exceeded")
        value = result[0]
        if isinstance(value, BaseException):
            raise OSError("DNS resolution failed") from value
        return value


_RESOLVER = _DaemonResolver()


class FixedOriginHttpsTransport:
    """HTTPS-only, no-redirect transport with one total monotonic deadline."""

    OFFICIAL_HOSTS = frozenset(
        {"api.telegram.org", "ilinkai.weixin.qq.com", "bots.qq.com", "api.sgroup.qq.com"}
    )

    def __init__(
        self,
        *,
        resolver: Callable[[str, int, float], list[Any]] = _RESOLVER,
        ssl_context: ssl.SSLContext | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        proxy_url: str | None = None,
    ) -> None:
        self.resolver = resolver
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.monotonic = monotonic
        # None discovers the standard HTTPS proxy; "" explicitly selects direct mode.
        self.proxy_url = proxy_url

    @staticmethod
    def _decode_proxy_userinfo(value: str) -> str:
        if re.search(r"%(?![0-9A-Fa-f]{2})", value):
            raise ValueError("invalid proxy URL")
        try:
            decoded = urllib.parse.unquote(value, encoding="utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("invalid proxy URL") from exc
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
            raise ValueError("invalid proxy URL")
        return decoded

    @staticmethod
    def _validate_proxy_host(host: str) -> None:
        if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in host):
            raise ValueError("invalid proxy URL")
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid proxy URL") from exc
        if len(ascii_host) > 253 or ascii_host.endswith("."):
            raise ValueError("invalid proxy URL")
        labels = ascii_host.split(".")
        if any(
            not 1 <= len(label) <= 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(not (char.isalnum() or char == "-") for char in label)
            for label in labels
        ):
            raise ValueError("invalid proxy URL")

    def _proxy_for(self, target_host: str) -> _ProxyConfig | None:
        raw = self.proxy_url
        if raw is None:
            no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
            if no_proxy:
                bypass = urllib.request.proxy_bypass_environment(  # type: ignore[attr-defined]
                    target_host, {"no": no_proxy}
                )
            else:
                bypass = urllib.request.proxy_bypass(target_host)
            if bypass:
                return None
            raw = urllib.request.getproxies().get("https", "")
        if not raw:
            return None
        if raw != raw.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
            raise ValueError("invalid proxy URL")
        try:
            parsed = urllib.parse.urlsplit(raw)
            port = parsed.port if parsed.port is not None else 80
        except ValueError as exc:
            raise ValueError("invalid proxy URL") from exc
        host = parsed.hostname
        if (
            parsed.scheme != "http"
            or not host
            or not 1 <= port <= 65535
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid proxy URL")
        self._validate_proxy_host(host)
        authorization = None
        if parsed.username is not None or parsed.password is not None:
            if parsed.username is None:
                raise ValueError("invalid proxy URL")
            username = self._decode_proxy_userinfo(parsed.username)
            password = self._decode_proxy_userinfo(parsed.password or "")
            if not username or ":" in username:
                raise ValueError("invalid proxy URL")
            token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            authorization = f"Basic {token}"
        return _ProxyConfig(host, port, authorization)

    @staticmethod
    def _open_proxy_tunnel(
        sock: socket.socket,
        target_host: str,
        proxy: _ProxyConfig,
        remaining: Callable[[], float],
    ) -> None:
        authority = f"{target_host}:443"
        lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if proxy.authorization is not None:
            lines.append(f"Proxy-Authorization: {proxy.authorization}")
        wire = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        sock.settimeout(remaining())
        sock.sendall(wire)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            sock.settimeout(remaining())
            chunk = sock.recv(min(4096, MAX_HTTP_HEADER_BYTES + 1 - len(response)))
            remaining()
            if not chunk:
                raise OSError("proxy tunnel failed")
            response.extend(chunk)
            if len(response) > MAX_HTTP_HEADER_BYTES:
                raise OSError("proxy tunnel failed")
        header, extra = bytes(response).split(b"\r\n\r\n", 1)
        if extra:
            raise OSError("proxy tunnel failed")
        first_line = header.split(b"\r\n", 1)[0]
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", first_line)
        if match is None or not 200 <= int(match.group(1)) < 300:
            raise OSError("proxy tunnel failed")

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout: float,
        deadline: float | None = None,
        max_response_bytes: int,
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.OFFICIAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError("request origin is not allowed")
        host = parsed.hostname
        assert host is not None
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
            raise ValueError("invalid request target")
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request is too large")
        if method not in {"POST"} or any(
            not key
            or key.casefold() == "proxy-authorization"
            or "\r" in key
            or "\n" in key
            or "\r" in value
            or "\n" in value
            for key, value in headers.items()
        ):
            raise ValueError("invalid request metadata")
        deadline = self.monotonic() + timeout if deadline is None else deadline
        expired = threading.Event()
        active: list[socket.socket] = []

        def remaining() -> float:
            budget = deadline - self.monotonic()
            if budget <= 0 or expired.is_set():
                raise TimeoutError("notification deadline exceeded")
            return budget

        def interrupt() -> None:
            expired.set()
            for current in tuple(active):
                try:
                    current.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                current.close()

        proxy = self._proxy_for(host)
        connect_host = proxy.host if proxy is not None else host
        connect_port = proxy.port if proxy is not None else 443
        addresses = self.resolver(connect_host, connect_port, remaining())
        remaining()
        sock: socket.socket | None = None
        response: http.client.HTTPResponse | None = None
        watchdog = threading.Timer(remaining(), interrupt)
        watchdog.name = "notification-http-deadline"
        watchdog.daemon = True
        watchdog.start()
        try:
            last_error: Exception | None = None
            for family, socktype, proto, _canonname, address in addresses:
                candidate = socket.socket(family, socktype, proto)
                active.append(candidate)
                try:
                    candidate.settimeout(remaining())
                    candidate.connect(address)
                    if proxy is not None:
                        self._open_proxy_tunnel(candidate, host, proxy, remaining)
                    candidate.settimeout(remaining())
                    tls_sock = self.ssl_context.wrap_socket(candidate, server_hostname=host)
                    if candidate in active:
                        active.remove(candidate)
                    sock = tls_sock
                    active.append(sock)
                    break
                except Exception as exc:
                    last_error = exc
                    candidate.close()
                    if candidate in active:
                        active.remove(candidate)
            if sock is None:
                message = "proxy tunnel failed" if proxy is not None else "connection failed"
                raise OSError(message) from last_error
            encoded_headers = dict(headers)
            encoded_headers.update(
                {"Host": host, "Content-Length": str(len(body)), "Connection": "close"}
            )
            request = [f"{method} {target} HTTP/1.1"]
            request.extend(f"{key}: {value}" for key, value in encoded_headers.items())
            wire = ("\r\n".join(request) + "\r\n\r\n").encode("utf-8") + body
            sock.settimeout(remaining())
            sock.sendall(wire)

            adapter = _ResponseSocket(sock)
            response = http.client.HTTPResponse(adapter)
            sock.settimeout(remaining())
            response.begin()
            assert adapter.file is not None
            adapter.file.finish_headers()
            if 300 <= response.status < 400:
                raise ValueError("redirect responses are forbidden")
            chunks: list[bytes] = []
            count = 0
            while True:
                sock.settimeout(remaining())
                chunk = response.read(min(65_536, max_response_bytes + 1 - count))
                remaining()
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > max_response_bytes:
                    raise ValueError("response is too large")
            return HttpResponse(response.status, b"".join(chunks))
        except Exception as exc:
            if expired.is_set() or self.monotonic() >= deadline:
                raise TimeoutError("notification deadline exceeded") from exc
            raise
        finally:
            watchdog.cancel()
            watchdog.join()
            if response is not None:
                response.close()
            if sock is not None:
                sock.close()


# Compatibility name retained for third-party imports; behavior is no longer urllib based.
UrllibTransport = FixedOriginHttpsTransport


def _safe_failure(platform: str, code: str, detail: str = "") -> SafeResult:
    # Detail is selected by this module, never copied from an exception/response.
    message = detail[:240] if detail else code.replace("_", " ")
    return SafeResult(False, code, message, platform)


def _json_response(response: HttpResponse) -> dict[str, Any] | None:
    if not 200 <= response.status < 300 or len(response.body) > MAX_RESPONSE_BYTES:
        return None
    try:
        value = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_weixin_success(data: dict[str, Any]) -> bool:
    codes = _weixin_error_codes(data)
    if codes is None:
        return False
    if codes:
        return all(value == 0 for value in codes)
    message_id = data.get("message_id")
    return (
        type(message_id) is int
        and message_id >= 0
        or type(message_id) is str
        and 0 < len(message_id) <= 256
    )


def _weixin_error_codes(data: dict[str, Any]) -> tuple[int, ...] | None:
    values = tuple(data[name] for name in ("ret", "errcode") if name in data)
    if any(type(value) is not int for value in values):
        return None
    return values


def _weixin_uniform_error(data: dict[str, Any], code: int) -> bool:
    values = _weixin_error_codes(data)
    return values is not None and bool(values) and all(value == code for value in values)


def _weixin_errmsg(data: dict[str, Any]) -> str:
    value = data.get("errmsg")
    return value.casefold().strip() if type(value) is str else ""


def _is_weixin_stale_session(data: dict[str, Any]) -> bool:
    if _weixin_uniform_error(data, -14):
        return True
    if not _weixin_uniform_error(data, -2):
        return False
    errmsg = data.get("errmsg")
    if errmsg is None:
        return True
    return type(errmsg) is str and _weixin_errmsg(data) in {"", "unknown error"}


def _is_weixin_prepare_failed(data: dict[str, Any]) -> bool:
    return _weixin_uniform_error(data, -2) and _weixin_errmsg(data) == "prepare failed"


def _is_weixin_rate_limit(data: dict[str, Any]) -> bool:
    """Recognize live iLink throttling without conflating its stale-context variant."""

    if _is_weixin_stale_session(data) or _is_weixin_prepare_failed(data):
        return False
    if not _weixin_uniform_error(data, -2):
        return False
    errmsg = _weixin_errmsg(data)
    return bool(errmsg) and any(marker in errmsg for marker in WEIXIN_RATE_LIMIT_MARKERS)


def load_credentials(path: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Load only fixed credential names; process environment always takes precedence.

    The optional file is parsed as inert dotenv-like data. It must be a bounded,
    mode-0600 regular file and may not be a symlink. No expansion or shell syntax
    is interpreted.
    """

    source = os.environ if environ is None else environ
    result = {key: value for key in _ALLOWED_ENV if (value := source.get(key, "")).strip()}
    if not path:
        return result
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_ENV_BYTES
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return result
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > MAX_ENV_BYTES
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                return result
            chunks: list[bytes] = []
            remaining = MAX_ENV_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(data) > MAX_ENV_BYTES:
            return result
        text = data.decode("utf-8")
    except (OSError, UnicodeError):
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in _ALLOWED_ENV and key not in result:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if value and "\x00" not in value and len(value) <= 8192:
                result[key] = value
    return result


def credential_source_state(config: NotificationConfig) -> dict[str, bool]:
    """Return only presence booleans for masked UI diagnostics."""

    credentials = load_credentials(config.env_file)
    return {
        "telegram": bool(credentials.get("TELEGRAM_BOT_TOKEN")),
        "weixin": bool(credentials.get("WEIXIN_TOKEN")),
        "qq": bool(credentials.get("QQ_APP_ID") and credentials.get("QQ_CLIENT_SECRET")),
    }


class NotificationSender:
    TELEGRAM_BASE = "https://api.telegram.org"
    WEIXIN_SEND_URL = "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage"
    QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    QQ_API_BASE = "https://api.sgroup.qq.com"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        credential_environ: dict[str, str] | None = None,
    ) -> None:
        self.clock = clock
        self.sleeper = sleeper
        # None preserves desktop environment precedence; backend callers pass {}.
        self.credential_environ = (
            None if credential_environ is None else dict(credential_environ)
        )
        if transport is None:
            self.monotonic = monotonic
            self.transport = UrllibTransport(monotonic=monotonic)
        elif isinstance(transport, FixedOriginHttpsTransport):
            self.transport = transport
            self.monotonic = transport.monotonic
        else:
            self.transport = transport
            self.monotonic = monotonic
        self._qq_token: str | None = None
        self._qq_expiry = 0.0
        self._qq_identity: tuple[str, str] | None = None
        self._qq_lock = threading.Lock()
        self._qq_sequence = 0

    def send_platform(self, platform: str, text: str, settings: AppSettings) -> SafeResult:
        if not settings.notifications.enabled:
            return _safe_failure(platform, "disabled", "Notifications are disabled")
        config = settings.notifications
        try:
            credentials = load_credentials(config.env_file, environ=self.credential_environ)
            deadline = self.monotonic() + float(config.timeout)
            if platform == "telegram":
                return self._telegram(text, config, credentials, deadline)
            if platform == "weixin":
                return self._weixin(text, config, credentials, deadline)
            if platform == "qq":
                return self._qq(text, config, credentials, deadline)
            return _safe_failure(platform, "invalid_platform")
        except TimeoutError:
            return _safe_failure(platform, "timeout", "Network request timed out")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                return _safe_failure(platform, "timeout", "Network request timed out")
            return _safe_failure(platform, "network_error", "Network request failed")
        except Exception as exc:  # transport implementations are dependency-injected
            return _safe_failure(
                platform, "network_error", f"Network request failed ({type(exc).__name__})"
            )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("notification deadline exceeded")
        return remaining

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        deadline: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        timeout = self._remaining(deadline)
        if isinstance(self.transport, FixedOriginHttpsTransport):
            return self.transport.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                timeout=timeout,
                deadline=deadline,
                max_response_bytes=max_response_bytes,
            )
        return self.transport.request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    def send_enabled(self, text: str, settings: AppSettings) -> tuple[SafeResult, ...]:
        config = settings.notifications
        if not config.enabled:
            return (_safe_failure("", "disabled", "Notifications are disabled"),)
        enabled = [
            name
            for name, value in (
                ("telegram", config.telegram.enabled),
                ("weixin", config.weixin.enabled),
                ("qq", config.qq.enabled),
            )
            if value
        ]
        results = []
        for name in enabled:
            try:
                results.append(self.send_platform(name, text, settings))
            except BaseException:
                # A failed adapter must not suppress delivery on another platform.
                results.append(_safe_failure(name, "network_error", "Notification failed"))
        return tuple(results)

    def _telegram(
        self,
        text: str,
        config: NotificationConfig,
        credentials: dict[str, str],
        deadline: float,
    ) -> SafeResult:
        platform = "telegram"
        target = config.telegram
        if not target.enabled:
            return _safe_failure(platform, "disabled")
        token = credentials.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return _safe_failure(platform, "missing_credentials", "TELEGRAM_BOT_TOKEN is not set")
        if not target.chat_id.strip():
            return _safe_failure(platform, "missing_target", "Telegram chat ID is required")
        payload: dict[str, Any] = {"chat_id": target.chat_id.strip(), "text": text[:4096]}
        if target.thread_id is not None:
            payload["message_thread_id"] = target.thread_id
        response = self._request(
            "POST",
            f"{self.TELEGRAM_BASE}/bot{token}/sendMessage",
            headers={"Content-Type": "application/json"},
            json_body=payload,
            deadline=deadline,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        data = _json_response(response)
        if data is None or data.get("ok") is not True or not isinstance(data.get("result"), dict):
            return _safe_failure(platform, "api_error", "Telegram rejected the request")
        return SafeResult(True, "sent", "Telegram notification sent", platform)

    @staticmethod
    def _context_path(config: NotificationConfig, account_id: str) -> Path:
        if not _SAFE_ACCOUNT_ID.fullmatch(account_id) or account_id in {".", ".."}:
            raise ValueError("invalid Weixin account ID")

        def contained(base: Path) -> Path:
            root = base.expanduser().resolve(strict=False)
            candidate = root / f"{account_id}.context-tokens.json"
            if candidate.parent.resolve(strict=False) != root:
                raise ValueError("invalid Weixin account ID")
            return candidate

        configured = config.weixin.context_store.strip()
        if configured:
            path = Path(configured).expanduser()
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return path
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("Weixin context store must not be a symlink")
            return contained(path) if stat.S_ISDIR(metadata.st_mode) else path
        return contained(config_directory() / "weixin")

    @classmethod
    def _context_token(cls, config: NotificationConfig, account_id: str, peer: str) -> str:
        try:
            path = cls._context_path(config, account_id)
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                return ""
            if metadata.st_size > MAX_ENV_BYTES:
                return ""
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_size > MAX_ENV_BYTES
                    or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
                    or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                ):
                    return ""
                chunks: list[bytes] = []
                remaining = MAX_ENV_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(descriptor)
            if len(data) > MAX_ENV_BYTES:
                return ""
            raw = json.loads(data.decode("utf-8"))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return ""
        if not isinstance(raw, dict):
            return ""
        value = raw.get(peer)
        if not isinstance(value, str):
            account = raw.get(account_id)
            value = account.get(peer) if isinstance(account, dict) else ""
        return value if isinstance(value, str) and 0 < len(value) <= 8192 else ""

    def _weixin(
        self,
        text: str,
        config: NotificationConfig,
        credentials: dict[str, str],
        deadline: float,
    ) -> SafeResult:
        platform = "weixin"
        target_config = config.weixin
        if not target_config.enabled:
            return _safe_failure(platform, "disabled")
        token = credentials.get("WEIXIN_TOKEN", "")
        account = target_config.account_id.strip() or credentials.get("WEIXIN_ACCOUNT_ID", "")
        target = target_config.target.strip() or credentials.get("WEIXIN_HOME_CHANNEL", "")
        if not token or not account:
            return _safe_failure(
                platform, "missing_credentials", "WEIXIN_TOKEN and account ID are required"
            )
        if not _SAFE_ACCOUNT_ID.fullmatch(account) or account in {".", ".."}:
            return _safe_failure(platform, "invalid_settings", "Weixin account ID is invalid")
        if not target:
            return _safe_failure(platform, "missing_target", "Weixin target is required")
        context = self._context_token(config, account, target)
        if not context:
            return _safe_failure(
                platform,
                "missing_context_token",
                "No persisted context token for this account and peer; "
                "receive a message in the gateway first",
            )
        payload = {
            "base_info": {"channel_version": "2.2.0"},
            "msg": {
                "from_user_id": "",
                "to_user_id": target,
                "client_id": f"comfy-progress-weixin-{secrets.token_hex(16)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text[:4096]}}],
                "context_token": context,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": base64.b64encode(
                str(struct.unpack(">I", secrets.token_bytes(4))[0]).encode("ascii")
            ).decode("ascii"),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str((2 << 16) | (2 << 8)),
        }

        def send(body: dict[str, Any]) -> HttpResponse:
            return self._request(
                "POST",
                self.WEIXIN_SEND_URL,
                headers=headers,
                json_body=body,
                deadline=deadline,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )

        body = payload
        stale_retried = False
        rate_limit_retries = 0
        while True:
            data = _json_response(send(body))
            if data is not None and _is_weixin_success(data):
                return SafeResult(True, "sent", "Weixin notification sent", platform)
            if (
                data is not None
                and not stale_retried
                and "context_token" in body["msg"]
                and _is_weixin_stale_session(data)
            ):
                retry_message = dict(body["msg"])
                retry_message.pop("context_token")
                body = {"base_info": body["base_info"], "msg": retry_message}
                stale_retried = True
                continue
            if (
                data is not None
                and _is_weixin_prepare_failed(data)
            ):
                return _safe_failure(
                    platform,
                    "missing_context_token",
                    "Weixin context token is not ready; ask the user to send a message first",
                )
            if (
                data is not None
                and rate_limit_retries < len(WEIXIN_RATE_LIMIT_BACKOFF)
                and _is_weixin_rate_limit(data)
            ):
                delay = min(
                    WEIXIN_RATE_LIMIT_BACKOFF[rate_limit_retries], self._remaining(deadline)
                )
                rate_limit_retries += 1
                self.sleeper(delay)
                self._remaining(deadline)
                continue
            return _safe_failure(platform, "api_error", "Weixin rejected the request")

    def _qq_access_token(
        self,
        config: NotificationConfig,
        app_id: str,
        secret: str,
        deadline: float,
    ) -> str | None:
        identity = (app_id, secret)
        with self._qq_lock:
            now = self.clock()
            if self._qq_token and self._qq_identity == identity and now < self._qq_expiry - 30:
                return self._qq_token
            response = self._request(
                "POST",
                self.QQ_TOKEN_URL,
                headers={"Content-Type": "application/json"},
                json_body={"appId": app_id, "clientSecret": secret},
                deadline=deadline,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
            data = _json_response(response)
            token = data.get("access_token") if data else None
            expires = data.get("expires_in", 0) if data else 0
            if not isinstance(token, str) or not token or not isinstance(expires, (int, str)):
                return None
            try:
                lifetime = max(1, min(int(expires), 86_400))
            except ValueError:
                return None
            self._qq_token = token
            self._qq_expiry = now + lifetime
            self._qq_identity = identity
            return token

    def _qq(
        self,
        text: str,
        config: NotificationConfig,
        credentials: dict[str, str],
        deadline: float,
    ) -> SafeResult:
        platform = "qq"
        target_config = config.qq
        if not target_config.enabled:
            return _safe_failure(platform, "disabled")
        app_id = credentials.get("QQ_APP_ID", "")
        secret = credentials.get("QQ_CLIENT_SECRET", "")
        if not app_id or not secret:
            return _safe_failure(
                platform, "missing_credentials", "QQ_APP_ID and QQ_CLIENT_SECRET are required"
            )
        target = target_config.target.strip()
        if not target:
            return _safe_failure(platform, "missing_target", "QQ target is required")
        token = self._qq_access_token(config, app_id, secret, deadline)
        if not token:
            return _safe_failure(platform, "auth_error", "QQ authentication failed")
        encoded_target = urllib.parse.quote(target, safe="")
        paths = {
            "c2c": f"/v2/users/{encoded_target}/messages",
            "group": f"/v2/groups/{encoded_target}/messages",
            "channel": f"/channels/{encoded_target}/messages",
        }
        if target_config.target_type == "channel":
            payload: dict[str, Any] = {"content": text[:4000]}
        else:
            with self._qq_lock:
                self._qq_sequence = self._qq_sequence % 1_000_000 + 1
                sequence = self._qq_sequence
            payload = {"content": text[:4000], "msg_type": 0, "msg_seq": sequence}
        response = self._request(
            "POST",
            self.QQ_API_BASE + paths[target_config.target_type],
            headers={"Content-Type": "application/json", "Authorization": f"QQBot {token}"},
            json_body=payload,
            deadline=deadline,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        data = _json_response(response)
        if data is None or not isinstance(data.get("id"), str):
            return _safe_failure(platform, "api_error", "QQ rejected the request")
        return SafeResult(True, "sent", "QQ notification sent", platform)


@dataclass
class _EndpointProgress:
    """Constant-size exact-once state for one configured host/port."""

    instance: EndpointId
    notified_busy_epoch: int = 0
    latest_observed_at: float | None = None


@dataclass(frozen=True)
class _PendingRetry:
    """Completion rejected solely because the bounded work queue was full."""

    instance: EndpointId
    busy_epoch: int
    observed_at: float | None = None


class CompletionDispatcher:
    """Bounded one-worker dispatcher with atomic enqueue/deduplication state."""

    def __init__(
        self,
        sender: CompletionSender,
        settings: AppSettings,
        *,
        audio: Any = None,
        max_queue: int = 32,
        dedupe_size: int | None = None,
    ) -> None:
        self.sender = sender
        self.settings = settings
        self.audio = audio
        self.max_queue = max(1, min(max_queue, 256))
        self.dedupe_size = None if dedupe_size is None else max(1, min(dedupe_size, 4096))
        self._queue: queue.Queue[tuple[str, AppSettings]] = queue.Queue(self.max_queue)
        self._endpoint_progress: dict[tuple[str, int], _EndpointProgress] = {}
        self._pending_retry: dict[tuple[str, int], _PendingRetry] = {}
        self._closed = False
        self._lock = threading.RLock()
        self.last_results: tuple[SafeResult, ...] = ()
        self._thread = threading.Thread(target=self._run, name="completion-notifier", daemon=True)
        self._thread.start()

    def update_settings(self, settings: AppSettings) -> None:
        with self._lock:
            self.settings = settings
            self._prune_removed_endpoints()

    def _prune_removed_endpoints(self) -> None:
        configured = {(item.host, item.port) for item in self.settings.endpoints}
        self._endpoint_progress = {
            address: progress
            for address, progress in self._endpoint_progress.items()
            if address in configured
        }
        self._pending_retry = {
            address: pending
            for address, pending in self._pending_retry.items()
            if address in configured
        }

    def dispatch(self, reduction: Reduction, endpoint_names: dict[tuple[str, int], str]) -> int:
        audio_configs = []
        count = 0
        with self._lock:
            if self._closed:
                return 0
            self._prune_removed_endpoints()
            configured = {(item.host, item.port) for item in self.settings.endpoints}
            replacements = {
                item.endpoint: item
                for item in reduction.transitions
                if item.kind == "instance_replaced"
            }
            completion_markers = {
                item.endpoint: item
                for item in reduction.transitions
                if item.kind == "queue_completed"
            }
            current = set(reduction.state.endpoints)
            for endpoint in reduction.state.endpoints:
                address = (endpoint.host, endpoint.port)
                if address not in configured:
                    continue
                progress = self._endpoint_progress.get(address)
                if progress is None:
                    self._endpoint_progress[address] = _EndpointProgress(endpoint)
                    # Creating tracking state (including after remove/re-add) must
                    # never inherit retry work from an earlier configuration.
                    self._pending_retry.pop(address, None)
                elif progress.instance != endpoint:
                    replacement = replacements.get(endpoint)
                    if replacement is None or replacement.observed_at is None:
                        continue
                    if (
                        progress.latest_observed_at is not None
                        and replacement.observed_at <= progress.latest_observed_at
                    ):
                        continue
                    self._endpoint_progress[address] = _EndpointProgress(
                        endpoint, latest_observed_at=replacement.observed_at
                    )
                    self._pending_retry.pop(address, None)
            # Only a reducer transition can create completion work. An ordinary
            # reduction can retry that work solely after explicit backpressure.
            for endpoint, state in reduction.state.endpoints.items():
                address = (endpoint.host, endpoint.port)
                progress = self._endpoint_progress.get(address)
                marker = completion_markers.get(endpoint)
                busy_epoch = state.busy_epoch
                pending = self._pending_retry.get(address)
                if pending is not None and (
                    pending.instance != endpoint or pending.busy_epoch != busy_epoch
                ):
                    # Once the accepted current generation/epoch has moved on, its
                    # old retry cannot become valid again.
                    if progress is not None and progress.instance == endpoint:
                        self._pending_retry.pop(address, None)
                    pending = None
                genuine_transition = marker is not None and marker.busy_epoch == busy_epoch
                retrying_backpressure = (
                    marker is None
                    and pending is not None
                    and pending.instance == endpoint
                    and pending.busy_epoch == busy_epoch
                )
                stale_marker = (
                    marker is not None
                    and marker.observed_at is not None
                    and progress is not None
                    and progress.latest_observed_at is not None
                    and marker.observed_at <= progress.latest_observed_at
                )
                if (
                    endpoint not in current
                    or state.online is not True
                    or state.busy
                    or progress is None
                    or progress.instance != endpoint
                    or busy_epoch <= 0
                    or busy_epoch <= progress.notified_busy_epoch
                    or stale_marker
                    or not (genuine_transition or retrying_backpressure)
                ):
                    continue
                settings = self.settings
                name = endpoint_names.get(address, f"{endpoint.host}:{endpoint.port}")
                text = completion_text(settings.language, name)
                try:
                    self._queue.put_nowait((text, settings))
                except queue.Full:
                    # Only a real transition may open a retry window. A failed retry
                    # leaves the existing marker intact.
                    if genuine_transition:
                        self._pending_retry[address] = _PendingRetry(
                            endpoint,
                            busy_epoch,
                            marker.observed_at if marker is not None else None,
                        )
                    continue
                progress.notified_busy_epoch = busy_epoch
                self._pending_retry.pop(address, None)
                if marker is not None and marker.observed_at is not None:
                    progress.latest_observed_at = marker.observed_at
                elif pending is not None and pending.observed_at is not None:
                    progress.latest_observed_at = pending.observed_at
                count += 1
                audio_configs.append(settings.audio)
        # QSoundEffect belongs to the GUI/caller thread, not the worker.
        if self.audio is not None:
            for config in audio_configs:
                self.audio.play(config)
        return count

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        return
                continue
            try:
                text, settings = item
                try:
                    results = self.sender.send_enabled(text, settings)
                    self.last_results = tuple(results)
                except Exception as exc:
                    self.last_results = (
                        _safe_failure("", "network_error", f"Sender failed ({type(exc).__name__})"),
                    )
            finally:
                self._queue.task_done()

    def shutdown(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._closed = True
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()


def completion_text(language: str, endpoint: str) -> str:
    templates = {
        "en-US": "{endpoint}: queue completed.",
        "zh-CN": "{endpoint}：队列已完成。",
        "ja-JP": "{endpoint}：キューが完了しました。",
        "ko-KR": "{endpoint}: 대기열이 완료되었습니다.",
    }
    return templates.get(language, templates["en-US"]).format(endpoint=endpoint[:256])
