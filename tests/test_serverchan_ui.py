"""First-run and secret lifecycle checks; all sends are in-memory fakes."""

import os
import threading
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox

from comfyui_progress_bridge.desktop import notifications
from comfyui_progress_bridge.desktop.i18n import Translator
from comfyui_progress_bridge.desktop.notifications import SafeResult
from comfyui_progress_bridge.desktop.secret_store import SendKeyStore
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    BackendNotificationSettings,
    ServerChanNotificationConfig,
    SettingsStore,
)
from comfyui_progress_bridge.desktop.widgets import ProgressWindow, SettingsDialog

FAKE_KEY = "SCTtestOnlyNotARealCredential123"


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fresh(tmp_path):
    # macOS /var and /tmp can be symlinks; a trusted test root must be canonical.
    root = tmp_path.resolve()
    return AppSettings(backend_notifications=BackendNotificationSettings(
        serverchan=ServerChanNotificationConfig(key_file=str(root / "secrets/serverchan.key")),
    ))


class MemoryStore:
    def __init__(self, key=""):
        self.key = key
        self.reads = 0
        self.writes = []
        self.deletes = 0

    def has_key(self):
        return bool(self.key)

    def load(self):
        self.reads += 1
        raise AssertionError("UI must not read a saved key")

    def save(self, key):
        self.writes.append(key)
        self.key = key

    def delete(self):
        self.deletes += 1
        self.key = ""


@pytest.mark.parametrize("language", ["zh-CN", "en-US", "ja-JP", "ko-KR"])
def test_opening_settings_only_checks_presence_and_never_prefills(app, fresh, language):
    secret = MemoryStore(FAKE_KEY)
    d = SettingsDialog(replace(fresh, language=language), secret_store=secret)
    assert d.serverchan_sendkey.text() == ""
    assert d.serverchan_sendkey.echoMode() == QLineEdit.EchoMode.Password
    assert not d.serverchan_sendkey.isEnabled()
    assert d.serverchan_key_status.text() == Translator(language)("configured")
    assert secret.reads == 0
    assert FAKE_KEY not in repr(d.result_settings())
    assert d.serverchan_sendkey.maxLength() == 1024
    d.reject()


def test_first_run_requires_explicit_key_and_opt_in(app, fresh):
    d = SettingsDialog(fresh, secret_store=MemoryStore())
    assert not d.backend_enabled.isChecked()
    assert not d.serverchan_enabled.isChecked()
    assert d.serverchan_sendkey.isEnabled()
    d.serverchan_enabled.setChecked(True)
    assert d.backend_enabled.isChecked()
    with pytest.raises(ValueError):
        d.validate_serverchan_key_action(d.result_settings())
    d.serverchan_sendkey.setText(FAKE_KEY)
    d.validate_serverchan_key_action(d.result_settings())
    d.reject()


def test_blank_keeps_saved_key_and_cancel_never_writes(app, fresh):
    secret = MemoryStore(FAKE_KEY)
    d = SettingsDialog(fresh, secret_store=secret)
    d._replace_serverchan_key()
    assert d.serverchan_sendkey.isEnabled()
    d.commit_serverchan_key()
    assert secret.writes == [] and secret.key == FAKE_KEY
    d.serverchan_sendkey.setText("SCTreplacement")
    d.reject()
    assert d.serverchan_sendkey.text() == ""
    assert secret.writes == [] and secret.key == FAKE_KEY


def test_save_uses_separate_secret_and_reopen_stays_blank(app, fresh, tmp_path):
    store = SettingsStore(tmp_path.resolve() / "settings.json")
    window = ProgressWindow(fresh, store=store)
    d = SettingsDialog(fresh)
    d.serverchan_enabled.setChecked(True)
    d.serverchan_sendkey.setText(FAKE_KEY)
    assert window.save_dialog_settings(d)
    assert d.serverchan_sendkey.text() == ""
    assert FAKE_KEY not in store.path.read_text()
    assert SendKeyStore(fresh.backend_notifications.serverchan.key_file).load() == FAKE_KEY
    saved = store.load()
    assert saved.backend_notifications.enabled
    assert saved.backend_notifications.serverchan.enabled
    reopened = SettingsDialog(saved)
    assert reopened.serverchan_sendkey.text() == ""
    assert reopened.serverchan_key_status.text() == Translator("en-US")("configured")
    reopened.reject()
    d.reject()
    window.close()


def test_public_settings_failure_does_not_write_key(app, fresh, monkeypatch):
    secret = MemoryStore()
    window = ProgressWindow(fresh)
    d = SettingsDialog(fresh, secret_store=secret)
    d.serverchan_sendkey.setText(FAKE_KEY)
    monkeypatch.setattr(window, "safe_save", lambda candidate: False)
    assert not window.save_dialog_settings(d)
    assert secret.writes == []
    assert d.serverchan_sendkey.text() == ""
    d.reject()
    window.close()


def test_secret_failure_restores_public_settings_without_key_in_error(
    app, fresh, tmp_path, monkeypatch,
):
    store = SettingsStore(tmp_path.resolve() / "settings.json")
    store.save(fresh)
    secret = MemoryStore()
    monkeypatch.setattr(secret, "save", lambda key: (_ for _ in ()).throw(ValueError(FAKE_KEY)))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    window = ProgressWindow(fresh, store=store)
    initialized_base = window.persisted_settings
    d = SettingsDialog(fresh, secret_store=secret)
    d.serverchan_sendkey.setText(FAKE_KEY)
    d.serverchan_enabled.setChecked(True)
    assert not window.save_dialog_settings(d)
    assert store.load() == initialized_base
    assert not window.settings.backend_notifications.serverchan.enabled
    assert d.serverchan_sendkey.text() == ""
    assert warnings and all(FAKE_KEY not in message for message in warnings)
    d.reject()
    window.close()


def test_explicit_delete_is_deferred_to_save_and_cancel_keeps_key(app, fresh, monkeypatch):
    secret = MemoryStore(FAKE_KEY)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    d = SettingsDialog(fresh, secret_store=secret)
    d._delete_serverchan_key()
    assert secret.key == FAKE_KEY
    d.reject()
    assert secret.key == FAKE_KEY and secret.deletes == 0
    d2 = SettingsDialog(fresh, secret_store=secret)
    d2._delete_serverchan_key()
    d2.commit_serverchan_key()
    assert secret.key == "" and secret.deletes == 1
    d2.reject()


def test_unsaved_key_test_uses_transient_override_without_saving(app, fresh, monkeypatch):
    captured = []
    called = threading.Event()

    class Sender:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def send_platform(self, platform, text, settings):
            captured.append((platform, settings))
            called.set()
            return SafeResult(True, "accepted", "accepted", "serverchan")

    monkeypatch.setattr(notifications, "NotificationSender", Sender)
    secret = MemoryStore()
    d = SettingsDialog(fresh, secret_store=secret)
    d.serverchan_enabled.setChecked(True)
    d.serverchan_sendkey.setText(FAKE_KEY)
    d._test_notification("serverchan", backend=True)
    assert called.wait(2)
    assert captured[0] == {"credential_environ": {}, "serverchan_sendkey": FAKE_KEY}
    assert captured[1][0] == "serverchan"
    assert captured[1][1].notifications.serverchan.enabled
    assert FAKE_KEY not in repr(captured[1][1])
    assert secret.writes == []
    d._show_test_result(SafeResult(True, "accepted", FAKE_KEY, "serverchan"))
    assert "confirm receipt" in d.validation_label.text()
    assert FAKE_KEY not in d.validation_label.text()
    d.reject()


def test_unsafe_storage_does_not_crash_settings_or_load_secret(app, fresh):
    secret = MemoryStore()
    secret.has_key = lambda: (_ for _ in ()).throw(ValueError(FAKE_KEY))
    d = SettingsDialog(fresh, secret_store=secret)
    assert "unsafe" in d.serverchan_key_status.text()
    assert FAKE_KEY not in d.serverchan_key_status.text()
    assert secret.reads == 0
    d.reject()
