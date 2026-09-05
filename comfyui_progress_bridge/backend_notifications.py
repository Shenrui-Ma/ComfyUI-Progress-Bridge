"""Opt-in queue-drained notifications that run inside the ComfyUI backend process."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from .desktop.settings import (
    AppSettings,
    BackendNotificationSettings,
    NotificationConfig,
    QQNotificationConfig,
    ServerChanNotificationConfig,
    TelegramNotificationConfig,
    WeixinNotificationConfig,
    config_directory,
)

BACKEND_CONFIG_ENV = "COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG"
MAX_BACKEND_CONFIG_BYTES = 1_048_576
MAX_BACKEND_CREDENTIAL_BYTES = 1_048_576
_INSTALL_LOCK = threading.Lock()


class _CompletionSender(Protocol):
    def send_enabled(self, text: str, settings: AppSettings) -> object: ...


@dataclass(frozen=True)
class BackendNotificationConfig:
    """Validated backend-only notification settings."""

    name: str
    settings: AppSettings


def _private_file(path: Path, max_bytes: int, *, non_secret_settings: bool = False) -> bytes:
    """Read a bounded regular file, retaining strict permissions for plaintext secrets.

    Windows chmod bits do not express ACL privacy. Non-secret settings use the
    user's normal AppData ACL there; ServerChan secrets are handled separately by
    SendKeyStore's user-bound DPAPI. Legacy plaintext credential files do not opt
    into this exception and retain the existing fail-closed policy.
    """
    require_private_mode = not (non_secret_settings and os.name == "nt")

    def unsafe(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISLNK(metadata.st_mode)
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            or not stat.S_ISREG(metadata.st_mode)
            or (require_private_mode and stat.S_IMODE(metadata.st_mode) != 0o600)
            or metadata.st_size > max_bytes
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        )

    try:
        metadata = path.lstat()
        if unsafe(metadata):
            raise ValueError("backend config must be a private regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if unsafe(opened) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev, metadata.st_ino
            ):
                raise ValueError("backend config must be a private regular file")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("backend config must be a private regular file") from exc
    if len(data) > max_bytes:
        raise ValueError("backend config is too large")
    return data


def _private_json(path: Path) -> dict[str, Any]:
    data = _private_file(path, MAX_BACKEND_CONFIG_BYTES, non_secret_settings=True)
    if len(data) > MAX_BACKEND_CONFIG_BYTES:
        raise ValueError("backend config is too large")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("backend config must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("backend config must be a JSON object")
    return value


def _object(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _only(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} setting")


def load_backend_notification_config(
    environ: Mapping[str, str] | None = None,
    *,
    default_path: Path | None = None,
) -> BackendNotificationConfig | None:
    """Load an explicitly selected host-local config, or return None when not opted in."""
    values = os.environ if environ is None else environ
    raw_path = values.get(BACKEND_CONFIG_ENV, "").strip()
    using_desktop_settings = not raw_path
    if using_desktop_settings:
        if default_path is None and environ is not None:
            return None
        path = default_path or config_directory() / "settings.json"
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("backend config must be a private regular file") from exc
    else:
        path = Path(raw_path).expanduser()
    config = _private_json(path)
    if using_desktop_settings:
        desktop_settings = AppSettings.from_dict(config)
        backend = desktop_settings.backend_notifications
        if not backend.enabled:
            return None
        return _backend_config_from_settings(backend, desktop_settings.language, path)

    _only(
        config,
        {
            "enabled",
            "name",
            "language",
            "credentials_file",
            "timeout",
            "telegram",
            "weixin",
            "serverchan",
        },
        "backend",
    )
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("backend enabled must be a bool")
    if not enabled:
        return None

    name = config.get("name", "ComfyUI")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("backend name must be a non-empty string")
    telegram = _object(config.get("telegram"), "telegram")
    _only(telegram, {"enabled", "chat_id", "thread_id"}, "telegram")
    telegram_config = TelegramNotificationConfig(
        enabled=telegram.get("enabled", False),
        chat_id=telegram.get("chat_id", ""),
        thread_id=telegram.get("thread_id"),
    )
    weixin = _object(config.get("weixin"), "weixin")
    _only(weixin, {"enabled", "account_id", "target", "context_store"}, "weixin")
    serverchan = _object(config.get("serverchan"), "serverchan")
    _only(serverchan, {"enabled", "key_file"}, "serverchan")
    backend = BackendNotificationSettings(
        enabled=True,
        name=name.strip(),
        credentials_file=config.get("credentials_file", ""),
        timeout=config.get("timeout", 10.0),
        telegram=telegram_config,
        serverchan=ServerChanNotificationConfig(**serverchan),
        weixin=WeixinNotificationConfig(
            enabled=weixin.get("enabled", False),
            account_id=weixin.get("account_id", ""),
            target=weixin.get("target", ""),
            context_store=weixin.get("context_store", ""),
        ),
    )
    return _backend_config_from_settings(backend, config.get("language", "en-US"), path)


def _backend_config_from_settings(
    backend: BackendNotificationSettings,
    language: str,
    source_path: Path,
) -> BackendNotificationConfig:
    credential_path = Path(backend.credentials_file).expanduser()
    if not credential_path.is_absolute():
        credential_path = source_path.parent / credential_path
    if backend.telegram.enabled or backend.weixin.enabled:
        _private_file(credential_path, MAX_BACKEND_CREDENTIAL_BYTES)
    key_path = Path(backend.serverchan.key_file).expanduser()
    if not key_path.is_absolute():
        key_path = source_path.parent / key_path
    context_store = backend.weixin.context_store
    if context_store and not Path(context_store).expanduser().is_absolute():
        context_store = str(source_path.parent / context_store)
    notifications = NotificationConfig(
        enabled=True,
        env_file=str(credential_path),
        timeout=backend.timeout,
        telegram=backend.telegram,
        serverchan=ServerChanNotificationConfig(backend.serverchan.enabled, str(key_path)),
        weixin=WeixinNotificationConfig(
            enabled=backend.weixin.enabled,
            account_id=backend.weixin.account_id,
            target=backend.weixin.target,
            context_store=context_store,
        ),
        qq=QQNotificationConfig(),
    )
    # Each platform validates its credentials, target and private context when sending.
    # An incomplete platform must not disable the other one or prevent later recovery.
    settings = AppSettings(language=language, notifications=notifications)
    return BackendNotificationConfig(backend.name, settings)


def _queue_remaining(event: object, data: object) -> int | None:
    if event != "status" or not isinstance(data, dict):
        return None
    status_value = data.get("status")
    if not isinstance(status_value, dict):
        return None
    exec_info = status_value.get("exec_info")
    if not isinstance(exec_info, dict):
        return None
    remaining = exec_info.get("queue_remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
        return None
    return remaining


class QueueDrainedNotifier:
    """Detect positive-to-zero queue transitions and notify on one daemon worker."""

    def __init__(
        self,
        sender: _CompletionSender,
        settings: AppSettings,
        name: str,
    ) -> None:
        from .desktop.notifications import completion_text

        self.sender = sender
        self.settings = settings
        self.text = completion_text(settings.language, name)
        self._armed = False
        self._closed = False
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._completed_epoch = 0
        self._attempted_epoch = 0
        self.worker = threading.Thread(
            target=self._run,
            name="comfy-progress-backend-notifications",
            daemon=True,
        )
        self.worker.start()

    def observe(self, event: object, data: object) -> bool:
        """Observe a send_sync call; never perform network work in the caller."""
        remaining = _queue_remaining(event, data)
        if remaining is None:
            return False
        with self._condition:
            if self._closed:
                return False
            if remaining > 0:
                self._armed = True
                return False
            if not self._armed:
                return False
            self._armed = False
            self._completed_epoch += 1
            self._condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed or self._attempted_epoch < self._completed_epoch
                )
                if self._attempted_epoch >= self._completed_epoch:
                    if self._closed:
                        return
                    continue
                self._attempted_epoch += 1
            try:
                self.sender.send_enabled(self.text, self.settings)
            except BaseException:
                # Messaging is best-effort and must never affect the ComfyUI process.
                pass

    def shutdown(self, timeout: float = 1.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self.worker.join(max(0.0, timeout))
        return not self.worker.is_alive()


def install_backend_notifications(
    prompt_server_class: type,
    config: BackendNotificationConfig,
    *,
    sender: _CompletionSender | None = None,
) -> bool:
    """Wrap PromptServer.send_sync while preserving its API and original delivery."""
    with _INSTALL_LOCK:
        current = prompt_server_class.send_sync
        if getattr(current, "_comfy_progress_backend_notifications", False):
            return False
        if sender is None:
            from .desktop.notifications import NotificationSender

            sender = NotificationSender(credential_environ={})
        notifier = QueueDrainedNotifier(sender, config.settings, config.name)

        @wraps(current)
        def send_sync_with_notifications(self, event, data, sid=None, *args, **kwargs):
            result = current(self, event, data, sid, *args, **kwargs)
            try:
                notifier.observe(event, data)
            except BaseException:
                pass
            return result

        send_sync_with_notifications._comfy_progress_backend_notifications = True  # type: ignore[attr-defined]
        send_sync_with_notifications._comfy_progress_backend_notifier = notifier  # type: ignore[attr-defined]
        prompt_server_class.send_sync = send_sync_with_notifications
        return True
