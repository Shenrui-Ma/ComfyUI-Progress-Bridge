import json
import os
import sys
import threading
import time
import types

import pytest

from comfyui_progress_bridge.backend_notifications import (
    BACKEND_CONFIG_ENV,
    QueueDrainedNotifier,
    _private_file,
    install_backend_notifications,
    load_backend_notification_config,
)
from comfyui_progress_bridge.bridge import install_bridge
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    BackendNotificationSettings,
    SettingsStore,
    TelegramNotificationConfig,
)


class RecordingSender:
    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate
        self.called = threading.Event()

    def send_enabled(self, text, settings):
        self.called.set()
        if self.gate is not None:
            self.gate.wait(1)
        self.calls.append((text, settings))
        return ()


def write_config(tmp_path, **overrides):
    credentials = tmp_path / "credentials.env"
    credentials.write_text("TELEGRAM_BOT_TOKEN=test-only\n")
    credentials.chmod(0o600)
    value = {
        "enabled": True,
        "name": "Render host",
        "language": "en-US",
        "credentials_file": str(credentials),
        "timeout": 4,
        "telegram": {"enabled": True, "chat_id": "-10042", "thread_id": 7},
        "weixin": {"enabled": False},
    }
    value.update(overrides)
    path = tmp_path / "backend-notifications.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def status(remaining):
    return {"status": {"exec_info": {"queue_remaining": remaining}}}


def test_backend_config_is_explicit_opt_in_and_builds_sender_settings(tmp_path):
    assert load_backend_notification_config({}) is None
    path = write_config(tmp_path)

    loaded = load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)})

    assert loaded is not None
    assert loaded.name == "Render host"
    assert loaded.settings.language == "en-US"
    assert loaded.settings.notifications.enabled is True
    assert loaded.settings.notifications.env_file == str(tmp_path / "credentials.env")
    assert loaded.settings.notifications.telegram.chat_id == "-10042"
    assert loaded.settings.notifications.qq.enabled is False


def test_backend_config_can_load_explicit_ui_managed_settings_file(tmp_path):
    credentials = tmp_path / "backend.env"
    credentials.write_text("TELEGRAM_BOT_TOKEN=test-only\n")
    credentials.chmod(0o600)
    settings_path = tmp_path / "settings.json"
    SettingsStore(settings_path).save(
        AppSettings(
            language="zh-CN",
            backend_notifications=BackendNotificationSettings(
                enabled=True,
                name="本机渲染",
                credentials_file=str(credentials),
                timeout=7,
                telegram=TelegramNotificationConfig(True, "chat", 5),
            ),
        )
    )

    loaded = load_backend_notification_config({}, default_path=settings_path)

    assert loaded is not None
    assert loaded.name == "本机渲染"
    assert loaded.settings.language == "zh-CN"
    assert loaded.settings.notifications.telegram.chat_id == "chat"


def test_ui_managed_backend_config_is_disabled_by_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    SettingsStore(settings_path).save(AppSettings())

    assert load_backend_notification_config({}, default_path=settings_path) is None


def test_missing_platform_credentials_or_target_do_not_disable_backend(tmp_path):
    path = write_config(tmp_path)
    credentials = tmp_path / "credentials.env"
    credentials.write_text("WEIXIN_TOKEN=not-a-telegram-token\n")
    credentials.chmod(0o600)

    assert load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)}) is not None

    credentials.write_text("TELEGRAM_BOT_TOKEN=test-only\n")
    path = write_config(
        tmp_path,
        telegram={"enabled": True, "chat_id": "", "thread_id": None},
    )
    assert load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)}) is not None


def test_unsafe_weixin_context_is_rejected_at_send_without_disabling_backend(tmp_path):
    from comfyui_progress_bridge.desktop.notifications import NotificationSender

    credentials = tmp_path / "credentials.env"
    credentials.write_text("WEIXIN_TOKEN=test-only\n")
    credentials.chmod(0o600)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"peer": "persisted-context"}))
    context.chmod(0o600)
    path = write_config(
        tmp_path,
        telegram={"enabled": False},
        weixin={
            "enabled": True,
            "account_id": "account",
            "target": "peer",
            "context_store": str(context),
        },
    )
    credentials.write_text("WEIXIN_TOKEN=test-only\n")
    credentials.chmod(0o600)

    assert load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)}) is not None
    context.chmod(0o644)
    loaded = load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)})
    assert loaded is not None
    sender = NotificationSender(transport=object(), credential_environ={})
    result = sender.send_platform("weixin", "done", loaded.settings)
    assert not result.ok
    assert result.code == "missing_context_token"


def test_backend_config_must_be_enabled_host_local_and_private(tmp_path):
    disabled = write_config(tmp_path, enabled=False)
    assert load_backend_notification_config({BACKEND_CONFIG_ENV: str(disabled)}) is None

    disabled.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        load_backend_notification_config({BACKEND_CONFIG_ENV: str(disabled)})

    disabled.chmod(0o600)
    link = tmp_path / "config-link.json"
    link.symlink_to(disabled)
    with pytest.raises(ValueError, match="private regular file"):
        load_backend_notification_config({BACKEND_CONFIG_ENV: str(link)})


@pytest.mark.parametrize("unsafe_kind", ["missing", "public", "symlink", "directory", "large"])
def test_backend_credentials_file_must_be_private_regular_and_bounded(tmp_path, unsafe_kind):
    path = write_config(tmp_path)
    credentials = tmp_path / "credentials.env"
    if unsafe_kind == "missing":
        credentials.unlink()
    elif unsafe_kind == "public":
        credentials.chmod(0o644)
    elif unsafe_kind == "symlink":
        real = tmp_path / "real-credentials.env"
        credentials.rename(real)
        credentials.symlink_to(real)
    elif unsafe_kind == "directory":
        credentials.unlink()
        credentials.mkdir()
    else:
        with credentials.open("wb") as stream:
            stream.truncate(1_048_577)

    with pytest.raises(ValueError, match="private regular file"):
        load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)})


@pytest.mark.parametrize("mode", [0o400, 0o700])
@pytest.mark.parametrize("target", ["config", "credentials"])
def test_backend_requires_exact_private_file_mode(tmp_path, mode, target):
    config = write_config(tmp_path)
    path = config if target == "config" else tmp_path / "credentials.env"
    path.chmod(mode)
    with pytest.raises(ValueError, match="private regular file"):
        load_backend_notification_config({BACKEND_CONFIG_ENV: str(config)})


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_backend_rejects_fifo_swapped_between_stat_and_open_without_blocking(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    original_open = os.open
    swapped = False

    def swap_and_open(candidate, flags):
        nonlocal swapped
        assert flags & os.O_NONBLOCK, "opening a replaced FIFO must never block"
        path.unlink()
        os.mkfifo(path, 0o600)
        swapped = True
        return original_open(candidate, flags)

    monkeypatch.setattr(os, "open", swap_and_open)
    with pytest.raises(ValueError, match="private regular file"):
        _private_file(path, 1024)
    assert swapped


def test_backend_private_file_reads_all_short_chunks(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    expected = path.read_bytes()
    original_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original_read(fd, min(size, 7)))
    assert _private_file(path, len(expected)) == expected


def test_backend_private_file_growth_stays_bounded(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    limit = path.stat().st_size
    requested = []

    def growing_read(fd, size):
        requested.append(size)
        return b"x" * size

    monkeypatch.setattr(os, "read", growing_read)
    with pytest.raises(ValueError, match="too large"):
        _private_file(path, limit)
    assert sum(requested) == limit + 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"unexpected": True}, "unknown backend setting"),
        ({"enabled": "yes"}, "enabled must be a bool"),
        ({"language": "xx"}, "unsupported language"),
        ({"timeout": 0}, "between 1 and 30"),
        ({"telegram": {"enabled": True, "chat_id": "chat", "typo": 1}}, "unknown telegram"),
        (
            {"weixin": {"enabled": True, "account_id": "a", "target": "b", "typo": 1}},
            "unknown weixin",
        ),
    ],
)
def test_backend_config_rejects_unknown_fields_and_invalid_types(tmp_path, overrides, message):
    path = write_config(tmp_path, **overrides)

    with pytest.raises(ValueError, match=message):
        load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)})


def test_queue_drained_state_machine_and_payload_validation(tmp_path):
    sender = RecordingSender()
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    notifier = QueueDrainedNotifier(sender, config.settings, config.name)
    try:
        assert notifier.observe("status", status(0)) is False
        assert notifier.observe("progress", status(2)) is False
        assert notifier.observe("status", {"exec_info": {"queue_remaining": 2}}) is False
        assert notifier.observe("status", status(True)) is False
        assert notifier.observe("status", status(-1)) is False

        assert notifier.observe("status", status(3)) is False
        assert notifier.observe("status", status(1)) is False
        assert notifier.observe("status", status(0)) is True
        assert notifier.observe("status", status(0)) is False
        assert notifier.observe("status", status(4)) is False
        assert notifier.observe("status", status(0)) is True
        assert notifier.shutdown(1)
    finally:
        notifier.shutdown(1)

    assert [text for text, _settings in sender.calls] == [
        "Render host: queue completed.",
        "Render host: queue completed.",
    ]


def test_network_sender_runs_in_daemon_worker_not_send_sync_callback(tmp_path):
    gate = threading.Event()
    sender = RecordingSender(gate)
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    notifier = QueueDrainedNotifier(sender, config.settings, config.name)
    try:
        notifier.observe("status", status(1))
        started = time.monotonic()
        assert notifier.observe("status", status(0)) is True
        assert time.monotonic() - started < 0.2
        assert sender.called.wait(1)
        assert notifier.worker.daemon is True
    finally:
        gate.set()
        assert notifier.shutdown(1)


def test_blocked_sender_does_not_drop_completed_epochs(tmp_path):
    gate = threading.Event()
    sender = RecordingSender(gate)
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    notifier = QueueDrainedNotifier(sender, config.settings, config.name)
    try:
        for _ in range(24):
            notifier.observe("status", status(1))
            assert notifier.observe("status", status(0)) is True
        gate.set()
        assert notifier.shutdown(3)
    finally:
        gate.set()
        notifier.shutdown(1)

    assert len(sender.calls) == 24


def test_sender_baseexception_is_fail_open_and_later_epochs_continue(tmp_path):
    class RaisingSender:
        def __init__(self):
            self.calls = 0
            self.delivered = threading.Event()

        def send_enabled(self, _text, _settings):
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("must stay inside notification worker")
            self.delivered.set()

    sender = RaisingSender()
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    notifier = QueueDrainedNotifier(sender, config.settings, config.name)
    try:
        for _ in range(2):
            notifier.observe("status", status(1))
            notifier.observe("status", status(0))
        assert sender.delivered.wait(1)
    finally:
        assert notifier.shutdown(1)
    assert sender.calls == 2


def test_installer_preserves_original_delivery_order_and_is_idempotent(tmp_path):
    calls = []

    class Server:
        def send_sync(self, event, data, sid=None):
            calls.append((event, data, sid))
            return "original-result"

    sender = RecordingSender()
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    assert install_backend_notifications(Server, config, sender=sender) is True
    assert install_backend_notifications(Server, config, sender=sender) is False

    server = Server()
    assert server.send_sync("status", status(2), "client") == "original-result"
    assert server.send_sync("status", status(0), "client") == "original-result"
    notifier = Server.send_sync._comfy_progress_backend_notifier
    assert notifier.shutdown(1)
    assert calls == [
        ("status", status(2), "client"),
        ("status", status(0), "client"),
    ]
    assert len(sender.calls) == 1


def test_installer_is_idempotent_under_concurrent_calls(monkeypatch, tmp_path):
    import comfyui_progress_bridge.backend_notifications as backend_module

    class Server:
        def send_sync(self, event, data, sid=None):
            return (event, data, sid)

    created = []

    class SlowNotifier:
        def __init__(self, *_args):
            created.append(self)
            time.sleep(0.05)

        def observe(self, *_args):
            return False

    monkeypatch.setattr(backend_module, "QueueDrainedNotifier", SlowNotifier)
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    start = threading.Barrier(3)
    results = []

    def install():
        start.wait()
        results.append(install_backend_notifications(Server, config, sender=RecordingSender()))

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(1)

    assert sorted(results) == [False, True]
    assert len(created) == 1


def test_installer_preserves_extended_positional_and_keyword_arguments(tmp_path):
    calls = []

    class Server:
        def send_sync(self, event, data, sid=None, *args, **kwargs):
            calls.append((event, data, sid, args, kwargs))
            return "original-result"

    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    assert install_backend_notifications(Server, config, sender=RecordingSender())

    payload = status(1)
    assert Server().send_sync("status", payload, "client", "extra", route="primary") == (
        "original-result"
    )
    notifier = Server.send_sync._comfy_progress_backend_notifier
    assert notifier.shutdown(1)
    assert calls == [("status", payload, "client", ("extra",), {"route": "primary"})]


def test_backend_and_udp_wrappers_together_preserve_extended_arguments(tmp_path):
    calls = []

    class Server:
        def send_sync(self, event, data, sid=None, *args, **kwargs):
            calls.append((event, data, sid, args, kwargs))
            return "original-result"

    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    assert config is not None
    assert install_bridge(
        Server,
        8188,
        udp_socket=type(
            "Socket",
            (),
            {
                "setblocking": lambda *_args: None,
                "sendto": lambda *_args: None,
            },
        )(),
    )
    assert install_backend_notifications(Server, config, sender=RecordingSender())

    payload = status(1)
    assert Server().send_sync("status", payload, "client", "extra", route="primary") == (
        "original-result"
    )
    notifier = Server.send_sync._comfy_progress_backend_notifier
    assert notifier.shutdown(1)
    assert calls == [("status", payload, "client", ("extra",), {"route": "primary"})]


def test_comfyui_installer_adds_backend_notifier_when_explicitly_configured(monkeypatch, tmp_path):
    import comfyui_progress_bridge

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy_module = types.ModuleType("comfy")
    cli_args_module = types.ModuleType("comfy.cli_args")
    cli_args_module.args = types.SimpleNamespace(port=8188)
    comfy_module.cli_args = cli_args_module
    server_module = types.ModuleType("server")
    server_module.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args_module)
    monkeypatch.setitem(sys.modules, "server", server_module)
    monkeypatch.setattr(comfyui_progress_bridge, "launch_desktop", lambda _port: False)
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    monkeypatch.setattr(comfyui_progress_bridge, "load_backend_notification_config", lambda: config)
    installed = []
    monkeypatch.setattr(
        comfyui_progress_bridge,
        "install_backend_notifications",
        lambda server, value: installed.append((server, value)) or True,
    )

    assert comfyui_progress_bridge.install_comfyui_bridge() is True
    assert installed == [(Server, config)]


def test_invalid_backend_config_is_fail_open_and_never_logs_exception_details(monkeypatch, capsys):
    import comfyui_progress_bridge

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy_module = types.ModuleType("comfy")
    cli_args_module = types.ModuleType("comfy.cli_args")
    cli_args_module.args = types.SimpleNamespace(port=8188)
    comfy_module.cli_args = cli_args_module
    server_module = types.ModuleType("server")
    server_module.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli_args_module)
    monkeypatch.setitem(sys.modules, "server", server_module)
    monkeypatch.setattr(comfyui_progress_bridge, "launch_desktop", lambda _port: False)

    def invalid_config():
        raise ValueError("DO-NOT-LOG-CONFIG-CONTENTS")

    monkeypatch.setattr(comfyui_progress_bridge, "load_backend_notification_config", invalid_config)

    assert comfyui_progress_bridge.install_comfyui_bridge() is True
    output = capsys.readouterr().out
    assert "backend notifications disabled" in output
    assert "DO-NOT-LOG-CONFIG-CONTENTS" not in output


def test_invalid_udp_config_does_not_disable_backend(monkeypatch, tmp_path):
    import comfyui_progress_bridge

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    comfy = types.ModuleType("comfy")
    cli = types.ModuleType("comfy.cli_args")
    cli.args = types.SimpleNamespace(port=8188)
    server = types.ModuleType("server")
    server.PromptServer = Server
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", cli)
    monkeypatch.setitem(sys.modules, "server", server)
    monkeypatch.setenv("COMFY_PROGRESS_BRIDGE_PORT", "invalid")
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    monkeypatch.setattr(comfyui_progress_bridge, "load_backend_notification_config", lambda: config)
    installed = []
    monkeypatch.setattr(
        comfyui_progress_bridge,
        "install_backend_notifications",
        lambda server, config: installed.append((server, config)) or True,
    )
    comfyui_progress_bridge.install_comfyui_bridge(desktop_launcher=lambda _: False)
    assert installed == [(Server, config)]


def test_backend_installed_sender_ignores_process_credentials(monkeypatch, tmp_path):
    from comfyui_progress_bridge.desktop.notifications import NotificationSender, SafeResult

    class Server:
        def send_sync(self, event, data, sid=None):
            return None

    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(write_config(tmp_path))})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unrelated-process-token")
    seen = []

    def record(self, text, settings, credentials, deadline):
        seen.append(credentials["TELEGRAM_BOT_TOKEN"])
        return SafeResult(True, "sent", "", "telegram")

    monkeypatch.setattr(NotificationSender, "_telegram", record)
    assert install_backend_notifications(Server, config)
    notifier = Server.send_sync._comfy_progress_backend_notifier
    try:
        notifier.sender.send_enabled("done", config.settings)
        assert seen == ["test-only"]
    finally:
        assert notifier.shutdown()


def test_incomplete_weixin_does_not_block_healthy_telegram(monkeypatch, tmp_path):
    from comfyui_progress_bridge.desktop.notifications import NotificationSender, SafeResult

    path = write_config(tmp_path, weixin={"enabled": True})
    config = load_backend_notification_config({BACKEND_CONFIG_ENV: str(path)})
    sender = NotificationSender(transport=object(), credential_environ={})
    monkeypatch.setattr(
        sender, "_telegram", lambda *args: SafeResult(True, "sent", "", "telegram")
    )
    results = sender.send_enabled("done", config.settings)
    assert results[0].ok
    assert results[1].platform == "weixin"
    assert not results[1].ok
