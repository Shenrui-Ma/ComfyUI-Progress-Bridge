import json
import os
import stat

import pytest

from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    AudioConfig,
    BackendNotificationSettings,
    EndpointConfig,
    NotificationConfig,
    QQNotificationConfig,
    ServerChanNotificationConfig,
    SettingsStore,
    TelegramNotificationConfig,
    WeixinNotificationConfig,
)


def test_secure_atomic_round_trip_and_unique_endpoints(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    settings = AppSettings(
        language="ja-JP",
        opacity=73,
        endpoints=(
            EndpointConfig("127.0.0.1", 8188, "GPU A", "#6C8EFF"),
            EndpointConfig(
                "127.0.0.1",
                8189,
                "GPU B",
                "#FF8A65",
                ssh_enabled=True,
                ssh_host="worker",
                ssh_user="comfy",
                ssh_port=2222,
            ),
        ),
    )
    SettingsStore(path).save(settings)
    assert SettingsStore(path).load() == settings
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))

    completion_settings = AppSettings(
        notifications=NotificationConfig(
            enabled=True,
            env_file="/private/credentials.env",
            timeout=3,
            telegram=TelegramNotificationConfig(True, "chat", 7),
            weixin=WeixinNotificationConfig(True, "account", "peer", "/private/context"),
            qq=QQNotificationConfig(True, "channel", "channel-id"),
        ),
        audio=AudioConfig(True, "custom", "/private/done.wav"),
        backend_notifications=BackendNotificationSettings(
            enabled=True,
            name="Render host",
            credentials_file="/private/backend-credentials.env",
            timeout=5,
            telegram=TelegramNotificationConfig(True, "backend-chat", 9),
            weixin=WeixinNotificationConfig(
                True, "backend-account", "backend-peer", "/private/backend-context"
            ),
        ),
    )
    SettingsStore(path).save(completion_settings)
    assert SettingsStore(path).load() == completion_settings
    serialized = path.read_text(encoding="utf-8")
    assert "BOT_TOKEN" not in serialized and "CLIENT_SECRET" not in serialized

    with pytest.raises(ValueError, match="host and port"):
        AppSettings(
            endpoints=(
                EndpointConfig("127.0.0.1", 8188, "a", "#ffffff"),
                EndpointConfig("127.0.0.1", 8188, "b", "#000000"),
            )
        )
    with pytest.raises(ValueError, match="names"):
        AppSettings(
            endpoints=(
                EndpointConfig("127.0.0.1", 1, "same", "#ffffff"),
                EndpointConfig("127.0.0.1", 2, "same", "#000000"),
            )
        )


def test_default_notification_paths_are_project_owned_not_gateway_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_PROGRESS_CONFIG_DIR", str(tmp_path))

    settings = AppSettings()

    assert settings.notifications.env_file == str(tmp_path / "notification-credentials.env")
    assert settings.backend_notifications.credentials_file == str(
        tmp_path / "backend-notification-credentials.env"
    )
    assert settings.backend_notifications.weixin.context_store == str(tmp_path / "weixin")
    assert settings.backend_notifications.serverchan.key_file == str(
        tmp_path / "secrets" / "serverchan.key"
    )
    assert ".hermes" not in repr(settings)


def test_serverchan_additive_schema_round_trip_and_legacy_defaults(tmp_path):
    path = tmp_path / "settings.json"
    serverchan = ServerChanNotificationConfig(True, "/private/secrets/serverchan.key")
    settings = AppSettings(
        notifications=NotificationConfig(serverchan=serverchan),
        backend_notifications=BackendNotificationSettings(
            enabled=True,
            credentials_file="",
            serverchan=serverchan,
        ),
    )
    SettingsStore(path).save(settings)
    assert SettingsStore(path).load() == settings
    assert "SendKey" not in path.read_text()
    raw = json.loads(path.read_text())
    del raw["notifications"]["serverchan"]
    raw["backend_notifications"] = {"enabled": False}
    legacy = AppSettings.from_dict(raw)
    assert not legacy.notifications.serverchan.enabled
    assert not legacy.backend_notifications.serverchan.enabled


@pytest.mark.parametrize("kwargs", [{"enabled": "true"}, {"key_file": ""}, {"key_file": None}])
def test_serverchan_settings_reject_invalid_types(kwargs):
    with pytest.raises(ValueError):
        ServerChanNotificationConfig(**kwargs)


def test_custom_settings_path_preserves_existing_parent_permissions(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "settings.json"

    SettingsStore(path).save(AppSettings())

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_config_recovers_defaults_and_quarantines_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version": 2, "opacity": 900}', encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded == AppSettings()
    assert list(tmp_path.glob("settings.json.invalid-*"))
    assert json.loads(path.read_text())["schema_version"] == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlink_and_oversized_settings_are_quarantined_without_reading_target(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("do not parse", encoding="utf-8")
    path = tmp_path / "settings.json"
    path.symlink_to(secret)
    assert SettingsStore(path).load() == AppSettings()
    assert secret.read_text(encoding="utf-8") == "do not parse"
    assert not path.is_symlink()

    path.write_bytes(b" " * 1_048_577)
    assert SettingsStore(path).load() == AppSettings()
    assert list(tmp_path.glob("settings.json.invalid-*"))


def test_schema_one_migration_and_override_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_PROGRESS_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "locale": "ko-KR",
                "alpha": 0.65,
                "simple": True,
                "hosts": [{"host": "localhost", "port": 8188, "name": "Local"}],
            }
        )
    )
    store = SettingsStore()
    loaded = store.load()
    assert loaded.language == "ko-KR"
    assert loaded.opacity == 65
    assert loaded.mode == "simple"
    assert loaded.endpoints[0].name == "Local"
    assert loaded.endpoints[0].host == "127.0.0.1"
    assert json.loads(path.read_text())["schema_version"] == 4


def test_schema_three_migration_adds_disabled_backend_notifications(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")

    loaded = SettingsStore(path).load()

    assert loaded.backend_notifications == BackendNotificationSettings()
    assert json.loads(path.read_text())["schema_version"] == 4


@pytest.mark.parametrize(
    "field,value", [("language", "xx"), ("mode", "huge"), ("theme", "pink"), ("opacity", 101)]
)
def test_settings_schema_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        AppSettings(**{field: value})


def test_endpoint_schema_validates_color_ssh_and_avatar_pngs():
    with pytest.raises(ValueError, match="color"):
        EndpointConfig(color="red")
    with pytest.raises(ValueError, match="ssh_host"):
        EndpointConfig(ssh_enabled=True)
    with pytest.raises(ValueError, match="PNGs"):
        AppSettings(avatar_paths=("avatar.jpg",))
    with pytest.raises(ValueError, match="six"):
        AppSettings(avatar_paths=tuple(f"{index}.png" for index in range(7)))


def test_numeric_hosts_runtime_avatar_and_secure_parent_reads(tmp_path):
    with pytest.raises(ValueError, match="numeric IPv4"):
        EndpointConfig(host="localhost")
    with pytest.raises(ValueError, match="numeric IPv4"):
        EndpointConfig(host="worker.example")

    path = tmp_path / "settings.json"
    SettingsStore(path).save(AppSettings(avatar_paths=("face.png",)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "avatar_index" not in payload

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    unsafe = SettingsStore(linked / "settings.json")
    assert unsafe.load() == AppSettings()
    with pytest.raises(ValueError, match="symlink"):
        unsafe.save(AppSettings())
    assert not (real / "settings.json").exists()

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "settings.fifo"
        os.mkfifo(fifo)
        assert SettingsStore(fifo).load() == AppSettings()
