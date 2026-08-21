import os
import threading
import time
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QLabel

from comfyui_progress_bridge.desktop.app import DesktopMonitor
from comfyui_progress_bridge.desktop.i18n import RESULT_MESSAGES, Translator, localized_result
from comfyui_progress_bridge.desktop.notifications import RESULT_CODES, SafeResult
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    AudioConfig,
    NotificationConfig,
    QQNotificationConfig,
    SettingsStore,
    TelegramNotificationConfig,
    WeixinNotificationConfig,
)
from comfyui_progress_bridge.desktop.widgets import ProgressWindow, SettingsDialog


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


class FakeNotificationSender:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()

    def send_platform(self, platform, text, settings):
        self.calls.append((platform, text, settings))
        self.called.set()
        return SafeResult(True, "sent", "mock sent", platform)


class FakeAudio:
    def __init__(self):
        self.calls = []

    def play(self, config):
        self.calls.append(config)
        return SafeResult(True, "played", "mock played", "audio")


def configured_settings(language="en-US"):
    return AppSettings(
        language=language,
        notifications=NotificationConfig(
            enabled=True,
            env_file="/private/credentials.env",
            timeout=4,
            telegram=TelegramNotificationConfig(True, "chat", 17),
            weixin=WeixinNotificationConfig(True, "account", "peer", "/private/context.json"),
            qq=QQNotificationConfig(True, "group", "group-id"),
        ),
        audio=AudioConfig(True, "custom", "/private/done.wav"),
    )


@pytest.mark.parametrize("language", ["en-US", "zh-CN", "ja-JP", "ko-KR"])
def test_task5_dialog_has_localized_targets_switches_and_test_controls(app, language):
    dialog = SettingsDialog(configured_settings(language))
    translator = Translator(language)
    labels = {label.text() for label in dialog.findChildren(QLabel)}
    for key in (
        "notifications_enabled",
        "credential_file",
        "telegram_target",
        "telegram_thread",
        "weixin_target",
        "weixin_account",
        "context_store",
        "qq_target",
        "qq_target_type",
        "audio_enabled",
        "audio",
        "wav_file",
    ):
        assert translator(key) in labels
    assert set(dialog.notification_test_buttons) == {"telegram", "weixin", "qq"}
    assert all(
        translator("test_notification") in button.text()
        for button in dialog.notification_test_buttons.values()
    )
    assert dialog.audio_test_button.text() == translator("test_audio")
    assert dialog.env_file.echoMode() == dialog.env_file.EchoMode.Password
    dialog.close()


def test_task5_dialog_round_trips_targets_and_audio(app):
    settings = configured_settings()
    dialog = SettingsDialog(settings)
    result = dialog.result_settings()
    assert result.notifications == settings.notifications
    assert result.audio == settings.audio
    dialog.close()


def test_notification_tests_only_send_after_explicit_button_click_and_run_off_ui_thread(app):
    sender = FakeNotificationSender()
    dialog = SettingsDialog(configured_settings(), notification_sender=sender)
    app.processEvents()
    assert sender.calls == []

    dialog.notification_test_buttons["telegram"].click()
    assert sender.called.wait(1)
    deadline = time.monotonic() + 1
    while not dialog.validation_label.text() and time.monotonic() < deadline:
        app.processEvents()
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "telegram"
    assert sender.calls[0][2].notifications.telegram.chat_id == "chat"
    assert localized_result("en-US", "sent") in dialog.validation_label.text()
    dialog.close()


class BlockingNotificationSender:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def send_platform(self, platform, text, settings):
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        return SafeResult(True, "sent", "raw adapter message", platform)


def test_notification_test_worker_is_single_bounded_busy_and_joined(app):
    baseline = sum(thread.name == "settings-test-worker" for thread in threading.enumerate())
    sender = BlockingNotificationSender()
    dialog = SettingsDialog(configured_settings("zh-CN"), notification_sender=sender)
    assert (
        sum(thread.name == "settings-test-worker" for thread in threading.enumerate())
        == baseline + 1
    )

    dialog._test_notification("telegram")
    assert sender.started.wait(1)
    assert all(not button.isEnabled() for button in dialog.notification_test_buttons.values())
    assert not dialog.audio_test_button.isEnabled()
    dialog._test_notification("qq")
    assert sender.calls == 1
    assert localized_result("zh-CN", "busy") in dialog.validation_label.text()

    started = time.monotonic()
    dialog.close()
    assert time.monotonic() - started < 0.6
    sender.release.set()
    deadline = time.monotonic() + 1
    while dialog._test_worker.thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not dialog._test_worker.thread.is_alive()


@pytest.mark.parametrize("language", ["en-US", "zh-CN", "ja-JP", "ko-KR"])
def test_every_adapter_result_code_is_localized_without_raw_message(app, language):
    assert set(RESULT_MESSAGES[language]) == RESULT_CODES
    dialog = SettingsDialog(configured_settings(language))
    try:
        for code in RESULT_CODES:
            dialog._show_test_result(
                SafeResult(code in {"sent", "played", "valid"}, code, "RAW ENGLISH ADAPTER")
            )
            displayed = dialog.validation_label.text()
            assert localized_result(language, code) in displayed
            assert "RAW ENGLISH ADAPTER" not in displayed
    finally:
        dialog.close()


def test_audio_test_only_plays_after_explicit_click(app):
    audio = FakeAudio()
    dialog = SettingsDialog(configured_settings(), audio_player=audio)
    assert audio.calls == []
    dialog.audio_test_button.click()
    assert audio.calls == [AudioConfig(True, "custom", "/private/done.wav")]
    assert localized_result("en-US", "played") in dialog.validation_label.text()
    dialog.close()


class RecordingDispatcher:
    def __init__(self):
        self.reductions = []
        self.settings = []
        self.shutdowns = 0

    def dispatch(self, reduction, names):
        self.reductions.append((reduction, names))
        return sum(item.kind == "queue_completed" for item in reduction.transitions)

    def update_settings(self, settings):
        self.settings.append(settings)

    def shutdown(self):
        self.shutdowns += 1
        return True


def test_desktop_controller_routes_only_reducer_queue_completion_and_shuts_worker(app, tmp_path):
    settings = AppSettings()
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    dispatcher = RecordingDispatcher()
    monitor = DesktopMonitor(window, settings, dispatcher=dispatcher)
    common = {
        "kind": "snapshot",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 1.0,
        "online": True,
        "pending_prompt_ids": [],
    }
    monitor.consume_record({**common, "running_prompt_ids": ["prompt"]})
    monitor.consume_record({**common, "running_prompt_ids": []})

    transition_kinds = [
        item.kind for reduction, _names in dispatcher.reductions for item in reduction.transitions
    ]
    assert transition_kinds == ["queue_completed"]
    assert dispatcher.reductions[-1][1] == {("127.0.0.1", 8188): "ComfyUI"}
    monitor.shutdown()
    assert dispatcher.shutdowns == 1
    window.close()
