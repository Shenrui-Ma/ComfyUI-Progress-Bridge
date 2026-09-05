"""Tests must never read credentials or persist settings in the developer's profile."""

import pytest


@pytest.fixture(autouse=True)
def isolate_user_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_PROGRESS_CONFIG_DIR", str(tmp_path.resolve() / "app-config"))
    for name in (
        "TELEGRAM_BOT_TOKEN", "WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_HOME_CHANNEL",
        "QQ_APP_ID", "QQ_CLIENT_SECRET", "COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
