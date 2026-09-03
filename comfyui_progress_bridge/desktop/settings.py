"""Validated, secure JSON settings for the standalone desktop monitor."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from comfyui_progress_bridge.monitor.source import build_ssh_argv

SCHEMA_VERSION = 4
MAX_SETTINGS_BYTES = 1_048_576
LANGUAGES = frozenset({"zh-CN", "ja-JP", "en-US", "ko-KR"})
THEMES = frozenset({"dark", "light", "system"})
MODES = frozenset({"simple", "professional"})
AUDIO_MODES = frozenset({"disabled", "ding", "custom"})
QQ_TARGET_TYPES = frozenset({"c2c", "group", "channel"})


class UnsafeSettingsPath(ValueError):
    """The configured path traverses a symlinked directory."""


def config_directory() -> Path:
    """Return a native per-user config directory, with a deterministic test override."""
    override = os.environ.get("COMFYUI_PROGRESS_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "comfyui-progress-bridge"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "comfyui-progress-bridge"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / (
        "comfyui-progress-bridge"
    )


def _text(value: object, name: str, *, required: bool = True) -> None:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > 1024:
        raise ValueError(f"{name} must be a valid string")


def _port(value: object, name: str = "port") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")


def validate_endpoint_host(value: object) -> str:
    """Return a canonical numeric IPv4 endpoint host or raise an actionable error."""
    _text(value, "host")
    try:
        address = str(ipaddress.IPv4Address(value))
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError("host must be a numeric IPv4 address (for example 127.0.0.1)") from exc
    if address != value:
        raise ValueError("host must be a canonical numeric IPv4 address")
    return address


@dataclass(frozen=True)
class EndpointConfig:
    host: str = "127.0.0.1"
    port: int = 8188
    name: str = "ComfyUI"
    color: str = "#6C8EFF"
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    ssh_identity_file: str = ""
    ssh_remote_python: str = "python3"
    ssh_probe_path: str = ""

    def __post_init__(self) -> None:
        validate_endpoint_host(self.host)
        _port(self.port)
        _text(self.name, "name")
        if (
            not isinstance(self.color, str)
            or len(self.color) != 7
            or not self.color.startswith("#")
        ):
            raise ValueError("color must be #RRGGBB")
        try:
            int(self.color[1:], 16)
        except ValueError as exc:
            raise ValueError("color must be #RRGGBB") from exc
        if not isinstance(self.ssh_enabled, bool):
            raise ValueError("ssh_enabled must be a bool")
        for value, name in (
            (self.ssh_host, "ssh_host"),
            (self.ssh_user, "ssh_user"),
            (self.ssh_identity_file, "ssh_identity_file"),
            (self.ssh_remote_python, "ssh_remote_python"),
            (self.ssh_probe_path, "ssh_probe_path"),
        ):
            _text(value, name, required=name == "ssh_remote_python")
        _port(self.ssh_port, "ssh_port")
        if self.ssh_enabled:
            if not self.ssh_host.strip():
                raise ValueError("ssh_host is required when SSH is enabled")
            probe = (
                [self.ssh_probe_path]
                if self.ssh_probe_path
                else ["-m", "comfyui_progress_bridge.monitor.remote_probe"]
            )
            try:
                build_ssh_argv(
                    host=self.ssh_host,
                    user=self.ssh_user,
                    port=self.ssh_port,
                    identity_file=self.ssh_identity_file or None,
                    remote_argv=[self.ssh_remote_python, *probe, f"{self.host}:{self.port}"],
                )
            except ValueError as exc:
                message = str(exc)
                field = next(
                    (
                        name
                        for marker, name in (
                            ("SSH host", "ssh_host"),
                            ("SSH user", "ssh_user"),
                            ("SSH port", "ssh_port"),
                            ("identity_file", "ssh_identity_file"),
                            ("remote_argv", "ssh_remote_python or ssh_probe_path"),
                        )
                        if marker in message
                    ),
                    "SSH settings",
                )
                raise ValueError(f"{field}: {message}") from exc


@dataclass(frozen=True)
class WindowPosition:
    screen: str = ""
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        _text(self.screen, "screen", required=False)
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must both be set or both be null")
        if self.x is not None:
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (self.x, self.y)):
                raise ValueError("window coordinates must be integers")


@dataclass(frozen=True)
class TelegramNotificationConfig:
    enabled: bool = False
    chat_id: str = ""
    thread_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("telegram enabled must be a bool")
        _text(self.chat_id, "Telegram chat ID", required=False)
        if self.thread_id is not None and (
            isinstance(self.thread_id, bool)
            or not isinstance(self.thread_id, int)
            or self.thread_id <= 0
        ):
            raise ValueError("Telegram thread ID must be a positive integer")


@dataclass(frozen=True)
class WeixinNotificationConfig:
    enabled: bool = False
    account_id: str = ""
    target: str = ""
    context_store: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Weixin enabled must be a bool")
        for value, name in (
            (self.account_id, "Weixin account ID"),
            (self.target, "Weixin target"),
            (self.context_store, "Weixin context store"),
        ):
            _text(value, name, required=False)


@dataclass(frozen=True)
class QQNotificationConfig:
    enabled: bool = False
    target_type: str = "c2c"
    target: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("QQ enabled must be a bool")
        if self.target_type not in QQ_TARGET_TYPES:
            raise ValueError("QQ target type must be c2c, group, or channel")
        _text(self.target, "QQ target", required=False)


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = False
    env_file: str = field(
        default_factory=lambda: str(config_directory() / "notification-credentials.env")
    )
    timeout: float = 10.0
    telegram: TelegramNotificationConfig = field(default_factory=TelegramNotificationConfig)
    weixin: WeixinNotificationConfig = field(default_factory=WeixinNotificationConfig)
    qq: QQNotificationConfig = field(default_factory=QQNotificationConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("notifications enabled must be a bool")
        _text(self.env_file, "credential environment file", required=False)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise ValueError("notification timeout must be a number")
        if not 1 <= self.timeout <= 30:
            raise ValueError("notification timeout must be between 1 and 30 seconds")
        if not isinstance(self.telegram, TelegramNotificationConfig):
            raise ValueError("telegram must be TelegramNotificationConfig")
        if not isinstance(self.weixin, WeixinNotificationConfig):
            raise ValueError("weixin must be WeixinNotificationConfig")
        if not isinstance(self.qq, QQNotificationConfig):
            raise ValueError("qq must be QQNotificationConfig")


@dataclass(frozen=True)
class BackendNotificationSettings:
    """Independent opt-in settings consumed by the ComfyUI backend process."""

    enabled: bool = False
    name: str = "ComfyUI"
    credentials_file: str = field(
        default_factory=lambda: str(config_directory() / "backend-notification-credentials.env")
    )
    timeout: float = 10.0
    telegram: TelegramNotificationConfig = field(default_factory=TelegramNotificationConfig)
    weixin: WeixinNotificationConfig = field(
        default_factory=lambda: WeixinNotificationConfig(
            context_store=str(config_directory() / "weixin")
        )
    )

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("backend notifications enabled must be a bool")
        _text(self.name, "backend name")
        _text(self.credentials_file, "backend credential file", required=False)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise ValueError("backend notification timeout must be a number")
        if not 1 <= self.timeout <= 30:
            raise ValueError("backend notification timeout must be between 1 and 30 seconds")
        if not isinstance(self.telegram, TelegramNotificationConfig):
            raise ValueError("backend telegram must be TelegramNotificationConfig")
        if not isinstance(self.weixin, WeixinNotificationConfig):
            raise ValueError("backend weixin must be WeixinNotificationConfig")
        if self.enabled:
            if not self.credentials_file.strip():
                raise ValueError("backend credential file is required when enabled")
            if not self.telegram.enabled and not self.weixin.enabled:
                raise ValueError("enable Telegram or Weixin for backend notifications")


@dataclass(frozen=True)
class AudioConfig:
    enabled: bool = False
    mode: str = "disabled"
    wav_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("audio enabled must be a bool")
        if self.mode not in AUDIO_MODES:
            raise ValueError("audio mode must be disabled, ding, or custom")
        _text(self.wav_path, "custom WAV path", required=False)


@dataclass(frozen=True)
class AppSettings:
    schema_version: int = SCHEMA_VERSION
    language: str = "en-US"
    mode: str = "professional"
    theme: str = "dark"
    opacity: int = 92
    collapsed: bool = False
    dock_enabled: bool = True
    position: WindowPosition = field(default_factory=WindowPosition)
    endpoints: tuple[EndpointConfig, ...] = field(default_factory=lambda: (EndpointConfig(),))
    avatar_enabled: bool = False
    avatar_paths: tuple[str, ...] = ()
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    backend_notifications: BackendNotificationSettings = field(
        default_factory=BackendNotificationSettings
    )

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        if self.language not in LANGUAGES:
            raise ValueError("unsupported language")
        if self.mode not in MODES:
            raise ValueError("unsupported mode")
        if self.theme not in THEMES:
            raise ValueError("unsupported theme")
        if (
            isinstance(self.opacity, bool)
            or not isinstance(self.opacity, int)
            or not 20 <= self.opacity <= 100
        ):
            raise ValueError("opacity must be an integer from 20 to 100")
        if not isinstance(self.collapsed, bool) or not isinstance(self.dock_enabled, bool):
            raise ValueError("collapsed and dock_enabled must be bools")
        if not isinstance(self.position, WindowPosition):
            raise ValueError("position must be a WindowPosition")
        if not isinstance(self.endpoints, tuple) or not self.endpoints:
            raise ValueError("at least one endpoint is required")
        if not all(isinstance(item, EndpointConfig) for item in self.endpoints):
            raise ValueError("endpoints must contain EndpointConfig values")
        addresses = [(item.host.casefold(), item.port) for item in self.endpoints]
        if len(addresses) != len(set(addresses)):
            raise ValueError("endpoint host and port pairs must be unique")
        names = [item.name.casefold() for item in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("endpoint names must be unique")
        if not isinstance(self.avatar_enabled, bool):
            raise ValueError("avatar_enabled must be a bool")
        if not isinstance(self.avatar_paths, tuple) or len(self.avatar_paths) > 6:
            raise ValueError("avatar_paths must contain at most six paths")
        for path in self.avatar_paths:
            _text(path, "avatar path")
            if Path(path).suffix.casefold() != ".png":
                raise ValueError("avatar files must be PNGs")
        if not isinstance(self.notifications, NotificationConfig):
            raise ValueError("notifications must be NotificationConfig")
        if not isinstance(self.audio, AudioConfig):
            raise ValueError("audio must be AudioConfig")
        if not isinstance(self.backend_notifications, BackendNotificationSettings):
            raise ValueError("backend_notifications must be BackendNotificationSettings")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppSettings:
        raw = dict(raw)
        # Compatibility with the short-lived Task 4 format. Avatar selection is
        # deliberately process-local and deterministic on every launch.
        raw.pop("avatar_index", None)
        allowed = {item.name for item in fields(cls)}
        if set(raw) - allowed:
            raise ValueError("settings contain unknown fields")
        data = dict(raw)
        endpoints = data.get("endpoints")
        if endpoints is not None:
            if not isinstance(endpoints, list):
                raise ValueError("endpoints must be a list")
            if not all(isinstance(item, dict) for item in endpoints):
                raise ValueError("endpoint entries must be objects")
            data["endpoints"] = tuple(EndpointConfig(**item) for item in endpoints)
        position = data.get("position")
        if position is not None:
            if not isinstance(position, dict):
                raise ValueError("position must be an object")
            data["position"] = WindowPosition(**position)
        avatars = data.get("avatar_paths")
        if avatars is not None:
            if not isinstance(avatars, list):
                raise ValueError("avatar_paths must be a list")
            data["avatar_paths"] = tuple(avatars)
        notifications = data.get("notifications")
        if notifications is not None:
            if not isinstance(notifications, dict):
                raise ValueError("notifications must be an object")
            notification_data = dict(notifications)
            for key, config_type in (
                ("telegram", TelegramNotificationConfig),
                ("weixin", WeixinNotificationConfig),
                ("qq", QQNotificationConfig),
            ):
                value = notification_data.get(key)
                if value is not None:
                    if not isinstance(value, dict):
                        raise ValueError(f"notifications.{key} must be an object")
                    notification_data[key] = config_type(**value)
            data["notifications"] = NotificationConfig(**notification_data)
        audio = data.get("audio")
        if audio is not None:
            if not isinstance(audio, dict):
                raise ValueError("audio must be an object")
            data["audio"] = AudioConfig(**audio)
        backend = data.get("backend_notifications")
        if backend is not None:
            if not isinstance(backend, dict):
                raise ValueError("backend_notifications must be an object")
            backend_data = dict(backend)
            for key, config_type in (
                ("telegram", TelegramNotificationConfig),
                ("weixin", WeixinNotificationConfig),
            ):
                value = backend_data.get(key)
                if value is not None:
                    if not isinstance(value, dict):
                        raise ValueError(f"backend_notifications.{key} must be an object")
                    backend_data[key] = config_type(**value)
            data["backend_notifications"] = BackendNotificationSettings(**backend_data)
        return cls(**data)


class SettingsStore:
    """Load and atomically save validated mode-0600 settings."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else config_directory() / "settings.json"

    def _validate_parent_components(self) -> None:
        """Reject existing symlinked parent components before file operations."""
        absolute = self.path.absolute()
        components = [absolute.parent, *absolute.parent.parents]
        for component in components:
            if component == component.parent:
                continue
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeSettingsPath(f"settings parent must not be a symlink: {component}")

    def _read_securely(self) -> str:
        self._validate_parent_components()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SETTINGS_BYTES:
                raise ValueError("settings must be a bounded regular file")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            chunks: list[bytes] = []
            remaining = MAX_SETTINGS_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_SETTINGS_BYTES:
                raise ValueError("settings file is too large")
            return payload.decode("utf-8")
        finally:
            os.close(descriptor)

    @staticmethod
    def _migrate(raw: object) -> tuple[dict[str, Any], bool]:
        if not isinstance(raw, dict):
            raise ValueError("settings root must be an object")
        version = raw.get("schema_version")
        if version == SCHEMA_VERSION:
            return raw, False
        if version in {2, 3}:
            migrated = dict(raw)
            migrated["schema_version"] = SCHEMA_VERSION
            migrated.setdefault("notifications", asdict(NotificationConfig()))
            migrated.setdefault("audio", asdict(AudioConfig()))
            migrated.setdefault("backend_notifications", asdict(BackendNotificationSettings()))
            return migrated, True
        if version != 1:
            raise ValueError("unsupported settings schema")
        hosts = raw.get("hosts", [])
        if not isinstance(hosts, list):
            raise ValueError("hosts must be a list")
        endpoints = []
        colors = ("#6C8EFF", "#FF8A65", "#66BB6A", "#AB77FF")
        for index, host in enumerate(hosts):
            if not isinstance(host, dict):
                raise ValueError("host entry must be an object")
            legacy_host = host.get("host", "127.0.0.1")
            # Schema 1 documented localhost; schema 2 accepts canonical numeric
            # IPv4 destinations only, avoiding DNS rebinding and ambiguity.
            if legacy_host == "localhost":
                legacy_host = "127.0.0.1"
            endpoints.append(
                {
                    "host": legacy_host,
                    "port": host.get("port", 8188),
                    "name": host.get("name", f"ComfyUI {index + 1}"),
                    "color": host.get("color", colors[index % len(colors)]),
                }
            )
        migrated = {
            "schema_version": SCHEMA_VERSION,
            "language": raw.get("locale", "en-US"),
            "mode": "simple" if raw.get("simple", False) else "professional",
            "opacity": round(raw.get("alpha", 0.92) * 100),
            "endpoints": endpoints or [asdict(EndpointConfig())],
            "notifications": asdict(NotificationConfig()),
            "audio": asdict(AudioConfig()),
            "backend_notifications": asdict(BackendNotificationSettings()),
        }
        return migrated, True

    def load(self) -> AppSettings:
        try:
            raw = json.loads(self._read_securely())
            migrated, changed = self._migrate(raw)
            settings = AppSettings.from_dict(migrated)
            if changed:
                self.save(settings)
            return settings
        except FileNotFoundError:
            return AppSettings()
        except UnsafeSettingsPath:
            return AppSettings()
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            invalid = self.path.with_name(f"{self.path.name}.invalid-{stamp}")
            try:
                os.replace(self.path, invalid)
            except OSError:
                pass
            recovered = AppSettings()
            try:
                self.save(recovered)
            except OSError:
                pass
            return recovered

    def save(self, settings: AppSettings) -> None:
        if not isinstance(settings, AppSettings):
            raise ValueError("settings must be AppSettings")
        self._validate_parent_components()
        missing: list[Path] = []
        candidate = self.path.parent
        while not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                # A racing actor created it; it is not ours to chmod.
                continue
            os.chmod(directory, 0o700)
        self._validate_parent_components()
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary: str | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
