import json
import re
import threading
import time
from dataclasses import replace
from uuid import UUID

import pytest

from comfyui_progress_bridge.desktop.notifications import (
    MAX_RESPONSE_BYTES,
    RESULT_CODES,
    CompletionDispatcher,
    HttpResponse,
    NotificationSender,
    SafeResult,
    completion_text,
    load_credentials,
)
from comfyui_progress_bridge.desktop.settings import (
    AppSettings,
    EndpointConfig,
    NotificationConfig,
    QQNotificationConfig,
    TelegramNotificationConfig,
    WeixinNotificationConfig,
)
from comfyui_progress_bridge.monitor.models import (
    EndpointId,
    EndpointState,
    MonitorState,
    Reduction,
    Transition,
)


@pytest.fixture(autouse=True)
def isolate_real_credentials(monkeypatch):
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "WEIXIN_TOKEN",
        "WEIXIN_ACCOUNT_ID",
        "WEIXIN_HOME_CHANNEL",
        "QQ_APP_ID",
        "QQ_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(value, status=200):
    return HttpResponse(status, json.dumps(value).encode())


def env_file(tmp_path, **values):
    path = tmp_path / "credentials.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    path.chmod(0o600)
    return str(path)


def _write_private_context(path, payload):
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def settings_for(tmp_path, *, telegram=None, weixin=None, qq=None, timeout=10):
    config = NotificationConfig(
        enabled=True,
        env_file=str(tmp_path / "credentials.env"),
        timeout=timeout,
        telegram=telegram or TelegramNotificationConfig(),
        weixin=weixin or WeixinNotificationConfig(),
        qq=qq or QQNotificationConfig(),
    )
    return AppSettings(notifications=config)


def test_telegram_uses_official_sendmessage_json_and_bounded_transport(tmp_path):
    token = "123456:super-secret"
    env_file(tmp_path, TELEGRAM_BOT_TOKEN=token)
    transport = FakeTransport([response({"ok": True, "result": {"message_id": 7}})])
    sender = NotificationSender(transport)
    settings = settings_for(
        tmp_path,
        telegram=TelegramNotificationConfig(True, "-10042", 99),
        timeout=3,
    )

    result = sender.send_platform("telegram", "done", settings)

    assert result == SafeResult(True, "sent", "Telegram notification sent", "telegram")
    method, url, call = transport.calls[0]
    assert method == "POST"
    assert url == f"https://api.telegram.org/bot{token}/sendMessage"
    assert call["headers"] == {"Content-Type": "application/json"}
    assert call["json_body"] == {
        "chat_id": "-10042",
        "text": "done",
        "message_thread_id": 99,
    }
    assert 0 < call["timeout"] <= 3
    assert call["max_response_bytes"] == MAX_RESPONSE_BYTES


def test_weixin_reuses_persisted_context_and_only_calls_sendmessage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "comfyui_progress_bridge.desktop.notifications.secrets.token_hex", lambda _n: "c" * 32
    )
    monkeypatch.setattr(
        "comfyui_progress_bridge.desktop.notifications.secrets.token_bytes", lambda _n: b"\0\0\0\1"
    )
    env_file(tmp_path, WEIXIN_TOKEN="wx-secret")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-context-secret"})
    transport = FakeTransport([response({"ret": 0})])
    sender = NotificationSender(transport)
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    result = sender.send_platform("weixin", "完成", settings)

    assert result.ok
    assert len(transport.calls) == 1
    _, url, call = transport.calls[0]
    assert url == "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage"
    assert "getupdates" not in url.casefold()
    assert call["headers"] == {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer wx-secret",
        "X-WECHAT-UIN": "MQ==",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "131584",
    }
    assert call["json_body"] == {
        "base_info": {"channel_version": "2.2.0"},
        "msg": {
            "from_user_id": "",
            "to_user_id": "peer",
            "client_id": "comfy-progress-weixin-" + "c" * 32,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": "完成"}}],
            "context_token": "persisted-context-secret",
        },
    }


@pytest.mark.parametrize(
    "stale_response",
    [
        {"ret": -14},
        {"ret": -2, "errcode": -2, "errmsg": "unknown error"},
    ],
    ids=["session-expired-minus-14", "stale-minus-2-unknown-error"],
)
def test_weixin_stale_context_retries_once_without_token(tmp_path, monkeypatch, stale_response):
    monkeypatch.setattr(
        "comfyui_progress_bridge.desktop.notifications.secrets.token_hex", lambda _n: "c" * 32
    )
    monkeypatch.setattr(
        "comfyui_progress_bridge.desktop.notifications.secrets.token_bytes", lambda _n: b"\0\0\0\1"
    )
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response(stale_response), response({"ret": 0})])
    ticks = iter([0.0, 0.0, 1.25])
    sender = NotificationSender(transport, monotonic=lambda: next(ticks))
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
        timeout=2,
    )

    result = sender.send_platform("weixin", "done", settings)

    assert result == SafeResult(True, "sent", "Weixin notification sent", "weixin")
    assert len(transport.calls) == 2
    first = transport.calls[0][2]
    second = transport.calls[1][2]
    assert first["json_body"]["msg"]["context_token"] == "persisted-test-context"
    assert "context_token" not in second["json_body"]["msg"]
    assert {
        key: value for key, value in first["json_body"]["msg"].items() if key != "context_token"
    } == second["json_body"]["msg"]
    assert first["json_body"]["base_info"] == second["json_body"]["base_info"]
    assert transport.calls[0][:2] == transport.calls[1][:2]
    assert first["headers"] == second["headers"]
    assert first["timeout"] == 2
    assert second["timeout"] == pytest.approx(0.75)


def test_weixin_accepts_zero_errcode_when_ret_is_omitted(tmp_path):
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"errcode": 0})])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result == SafeResult(True, "sent", "Weixin notification sent", "weixin")
    assert len(transport.calls) == 1


def test_weixin_accepts_live_message_id_only_success_response(tmp_path):
    """iLink sendmessage currently returns only a message_id on success."""

    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"message_id": "server-assigned-id"})])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result == SafeResult(True, "sent", "Weixin notification sent", "weixin")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "rate_limit_response",
    [{"ret": -2, "errmsg": "rate limited"}, {"errcode": -2, "errmsg": "frequency limit"}],
)
def test_weixin_rate_limit_retries_with_bounded_backoff_and_same_client_id(
    tmp_path, rate_limit_response
):
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport(
        [response(rate_limit_response), response(rate_limit_response), response({"ret": 0})]
    )
    sleeps = []
    sender = NotificationSender(transport, monotonic=lambda: 0.0, sleeper=sleeps.append)
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
        timeout=2,
    )

    result = sender.send_platform("weixin", "done", settings)

    assert result == SafeResult(True, "sent", "Weixin notification sent", "weixin")
    assert sleeps == [1.0, 2.0]
    messages = [call[2]["json_body"]["msg"] for call in transport.calls]
    assert len(messages) == 3
    assert len({message["client_id"] for message in messages}) == 1
    assert all(message["context_token"] == "persisted-test-context" for message in messages)


def test_weixin_rate_limit_retry_is_capped_and_keeps_result_code_api(tmp_path):
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"ret": -2, "errmsg": "rate limited"}) for _ in range(4)])
    sleeps = []
    sender = NotificationSender(transport, monotonic=lambda: 0.0, sleeper=sleeps.append)
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
        timeout=10,
    )

    result = sender.send_platform("weixin", "done", settings)

    assert result == SafeResult(False, "api_error", "Weixin rejected the request", "weixin")
    assert len(transport.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"ret": -14, "errcode": 0},
        {"ret": 0, "errcode": -14},
        {"ret": -2, "errcode": 0, "errmsg": "unknown error"},
        {"ret": 0, "errcode": -2, "errmsg": "Unknown Error"},
    ],
)
def test_weixin_mixed_contradictory_codes_fail_closed_without_retry(tmp_path, payload):
    env_file(tmp_path, **{"WEIXIN_TOKEN": "wx-test-token"})
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response(payload)])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result == SafeResult(False, "api_error", "Weixin rejected the request", "weixin")
    assert len(transport.calls) == 1
    assert "context_token" in transport.calls[0][2]["json_body"]["msg"]


def test_weixin_client_id_matches_known_working_ilink_shape(tmp_path):
    env_file(tmp_path, **{"WEIXIN_TOKEN": "wx-test-token"})
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"ret": 0})])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    assert NotificationSender(transport).send_platform("weixin", "done", settings).ok

    client_id = transport.calls[0][2]["json_body"]["msg"]["client_id"]
    assert re.fullmatch(r"comfy-progress-weixin-[0-9a-f]{32}", client_id)


def test_weixin_rate_limit_backoff_does_not_cross_notification_deadline(tmp_path):
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"errcode": -2, "errmsg": "frequency limit"})])
    now = {"value": 0.0, "calls": 0}
    sleeps = []

    def monotonic():
        value = now["value"]
        now["calls"] += 1
        if now["calls"] == 1:
            now["value"] = 0.95
        return value

    def sleep(delay):
        sleeps.append(delay)
        now["value"] += delay

    sender = NotificationSender(transport, monotonic=monotonic, sleeper=sleep)
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
        timeout=1,
    )

    result = sender.send_platform("weixin", "done", settings)

    assert result == SafeResult(False, "timeout", "Network request timed out", "weixin")
    assert len(transport.calls) == 1
    assert sleeps == pytest.approx([0.05])


def test_weixin_prepare_failed_is_missing_context_token_without_retry(tmp_path):
    env_file(tmp_path, WEIXIN_TOKEN="wx-test-token")
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {"peer": "persisted-test-context"})
    transport = FakeTransport([response({"ret": -2, "errmsg": "prepare failed"})])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(context)),
    )

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result.code == "missing_context_token"
    assert len(transport.calls) == 1


def test_weixin_tokenless_retry_failure_is_stable_and_secret_free(tmp_path):
    sensitive_values = (
        "persisted-test-context",
        "wx-test-token",
        "acct-sensitive",
        "peer-sensitive",
        "hostile-server-detail",
    )
    env_file(tmp_path, WEIXIN_TOKEN=sensitive_values[1])
    context = tmp_path / "acct.context-tokens.json"
    _write_private_context(context, {sensitive_values[3]: sensitive_values[0]})
    transport = FakeTransport(
        [
            response({"errcode": -14}),
            response({"ret": 9, "errmsg": sensitive_values[4], "echo": list(sensitive_values)}),
        ]
    )
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(
            True, sensitive_values[2], sensitive_values[3], str(context)
        ),
    )

    result = NotificationSender(transport).send_platform("weixin", "done", settings)

    assert result == SafeResult(False, "api_error", "Weixin rejected the request", "weixin")
    assert len(transport.calls) == 2
    first_message = transport.calls[0][2]["json_body"]["msg"]
    second_message = transport.calls[1][2]["json_body"]["msg"]
    assert first_message["context_token"] == sensitive_values[0]
    assert "context_token" not in second_message
    assert {
        key: value for key, value in first_message.items() if key != "context_token"
    } == second_message
    assert all(
        value not in repr(result) and value not in result.message for value in sensitive_values
    )


def test_weixin_without_persisted_context_never_makes_a_request(tmp_path):
    env_file(tmp_path, WEIXIN_TOKEN="wx-secret")
    transport = FakeTransport([])
    settings = settings_for(
        tmp_path,
        weixin=WeixinNotificationConfig(True, "acct", "peer", str(tmp_path / "missing")),
    )
    result = NotificationSender(transport).send_platform("weixin", "done", settings)
    assert result.code == "missing_context_token"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("target_type", "path", "expected_body"),
    [
        ("c2c", "/v2/users/target%2Fid/messages", {"content": "done", "msg_type": 0}),
        ("group", "/v2/groups/target%2Fid/messages", {"content": "done", "msg_type": 0}),
        ("channel", "/channels/target%2Fid/messages", {"content": "done"}),
    ],
)
def test_qq_token_cache_and_explicit_target_endpoint(tmp_path, target_type, path, expected_body):
    env_file(tmp_path, QQ_APP_ID="app", QQ_CLIENT_SECRET="client-secret")
    transport = FakeTransport(
        [
            response({"access_token": "access-secret", "expires_in": "3600"}),
            response({"id": "message-1"}),
            response({"id": "message-2"}),
        ]
    )
    clock = {"value": 100.0}
    sender = NotificationSender(transport, clock=lambda: clock["value"])
    settings = settings_for(
        tmp_path,
        qq=QQNotificationConfig(True, target_type, "target/id"),
    )

    assert sender.send_platform("qq", "done", settings).ok
    assert sender.send_platform("qq", "again", settings).ok

    assert len([call for call in transport.calls if call[1].endswith("getAppAccessToken")]) == 1
    first_send = transport.calls[1]
    assert first_send[1] == "https://api.sgroup.qq.com" + path
    assert first_send[2]["headers"]["Authorization"] == "QQBot access-secret"
    for key, value in expected_body.items():
        assert first_send[2]["json_body"][key] == value
    if target_type != "channel":
        assert first_send[2]["json_body"]["msg_seq"] == 1
        assert transport.calls[2][2]["json_body"]["msg_seq"] == 2


def test_qq_multi_request_operation_shares_one_deadline(tmp_path):
    env_file(tmp_path, QQ_APP_ID="app", QQ_CLIENT_SECRET="secret")
    transport = FakeTransport(
        [response({"access_token": "token", "expires_in": 60}), response({"id": "id"})]
    )
    values = iter([0.0, 0.0, 1.25])
    sender = NotificationSender(transport, monotonic=lambda: next(values))
    result = sender.send_platform(
        "qq",
        "done",
        settings_for(tmp_path, qq=QQNotificationConfig(True, "c2c", "peer"), timeout=2),
    )
    assert result.ok
    assert transport.calls[0][2]["timeout"] == 2
    assert transport.calls[1][2]["timeout"] == pytest.approx(0.75)


def test_disabled_missing_api_and_hostile_network_errors_are_secret_free(tmp_path):
    secret = "DO-NOT-LEAK-TOKEN"
    env_file(tmp_path, TELEGRAM_BOT_TOKEN=secret)
    enabled = settings_for(tmp_path, telegram=TelegramNotificationConfig(True, "chat"))
    hostile = FakeTransport([RuntimeError(f"failed URL contains {secret}")])
    result = NotificationSender(hostile).send_platform("telegram", "done", enabled)
    assert result.code == "network_error"
    assert secret not in repr(result) and secret not in result.message

    rejected = NotificationSender(
        FakeTransport([HttpResponse(401, secret.encode())])
    ).send_platform("telegram", "done", enabled)
    assert rejected.code == "api_error" and secret not in repr(rejected)

    disabled = replace(enabled, notifications=replace(enabled.notifications, enabled=False))
    transport = FakeTransport([])
    assert (
        NotificationSender(transport).send_platform("telegram", "done", disabled).code == "disabled"
    )
    assert transport.calls == []


def test_timeout_has_stable_result_code(tmp_path):
    env_file(tmp_path, TELEGRAM_BOT_TOKEN="token")
    settings = settings_for(tmp_path, telegram=TelegramNotificationConfig(True, "chat"))
    result = NotificationSender(
        FakeTransport([TimeoutError("secret timeout detail")])
    ).send_platform("telegram", "done", settings)
    assert result.code == "timeout"
    assert "secret timeout detail" not in result.message


def test_env_parser_is_inert_allowlisted_mode_0600_and_environment_wins(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text(
        "# comment\nTELEGRAM_BOT_TOKEN='file token'\n"
        "QQ_APP_ID=$(touch should-not-run)\nUNRELATED=ignored\nexport WEIXIN_TOKEN=nope\n"
    )
    path.chmod(0o600)
    values = load_credentials(str(path), {"TELEGRAM_BOT_TOKEN": "environment token"})
    assert values == {
        "TELEGRAM_BOT_TOKEN": "environment token",
        "QQ_APP_ID": "$(touch should-not-run)",
    }
    assert not (tmp_path / "should-not-run").exists()

    path.chmod(0o644)
    assert load_credentials(str(path), {}) == {}
    path.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(path)
    assert load_credentials(str(link), {}) == {}


def test_malformed_and_oversized_responses_are_rejected_without_body_echo(tmp_path):
    env_file(tmp_path, TELEGRAM_BOT_TOKEN="token")
    settings = settings_for(tmp_path, telegram=TelegramNotificationConfig(True, "chat"))
    for body in (b"not json", b"x" * (MAX_RESPONSE_BYTES + 1), b"[]"):
        result = NotificationSender(FakeTransport([HttpResponse(200, body)])).send_platform(
            "telegram", "done", settings
        )
        assert result.code == "api_error"
        assert "not json" not in result.message


def reduction(endpoint, *, busy, transitions=()):
    busy_epoch = max(
        (item.busy_epoch or 0 for item in transitions if item.kind == "queue_completed"),
        default=0,
    )
    endpoint_state = EndpointState(endpoint, online=True, busy=busy, busy_epoch=busy_epoch)
    return Reduction(MonitorState.from_parts({endpoint: endpoint_state}, {}, {}), transitions)


class RecordingSender:
    def __init__(self, gate=None):
        self.messages = []
        self.gate = gate

    def send_enabled(self, text, settings):
        if self.gate is not None:
            self.gate.wait(1)
        self.messages.append((text, settings.language))
        return ()


class RecordingAudio:
    def __init__(self):
        self.calls = []
        self.threads = []

    def play(self, config):
        self.calls.append(config)
        self.threads.append(threading.current_thread())
        return SafeResult(True, "played", "played", "audio")


def test_dispatcher_only_consumes_queue_completed_once_per_instance_busy_epoch():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    sender = RecordingSender()
    audio = RecordingAudio()
    dispatcher = CompletionDispatcher(sender, AppSettings(language="ja-JP"), audio=audio)
    try:
        assert dispatcher.dispatch(reduction(endpoint, busy=True), {}) == 0
        complete = reduction(
            endpoint,
            busy=False,
            transitions=(Transition("queue_completed", endpoint, busy_epoch=1),),
        )
        assert dispatcher.dispatch(complete, {("127.0.0.1", 8188): "GPU"}) == 1
        assert dispatcher.dispatch(complete, {}) == 0
        ignored = reduction(
            endpoint, busy=False, transitions=(Transition("instance_replaced", endpoint),)
        )
        assert dispatcher.dispatch(ignored, {}) == 0
        assert dispatcher.shutdown(1)
    finally:
        dispatcher.shutdown(1)
    assert sender.messages == [(completion_text("ja-JP", "GPU"), "ja-JP")]
    assert len(audio.calls) == 1
    assert audio.threads == [threading.current_thread()]


def test_dispatcher_new_busy_epoch_and_instance_are_independent_and_shutdown_drains_fifo():
    one = EndpointId("127.0.0.1", 8188, UUID(int=1))
    two = EndpointId("127.0.0.1", 8188, UUID(int=2))
    sender = RecordingSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=4)
    for index, endpoint in enumerate((one, one, two)):
        generation = (
            (Transition("instance_replaced", endpoint, observed_at=10.0),) if index == 2 else ()
        )
        dispatcher.dispatch(reduction(endpoint, busy=True, transitions=generation), {})
        assert (
            dispatcher.dispatch(
                reduction(
                    endpoint,
                    busy=False,
                    transitions=(
                        Transition("queue_completed", endpoint, busy_epoch=index % 2 + 1),
                    ),
                ),
                {},
            )
            == 1
        )
    assert dispatcher.shutdown(1)
    assert [text for text, _ in sender.messages] == [
        "127.0.0.1:8188: queue completed.",
        "127.0.0.1:8188: queue completed.",
        "127.0.0.1:8188: queue completed.",
    ]
    assert (
        dispatcher.dispatch(
            reduction(
                one, busy=False, transitions=(Transition("queue_completed", one, busy_epoch=1),)
            ),
            {},
        )
        == 0
    )


def test_dispatcher_never_pressure_evicts_exact_once_state_and_rejects_generation_replay():
    one = EndpointId("127.0.0.1", 8188, UUID(int=1))
    two = EndpointId("127.0.0.1", 8188, UUID(int=2))
    sender = RecordingSender()
    dispatcher = CompletionDispatcher(sender, AppSettings(), max_queue=32, dedupe_size=1)
    first_complete = reduction(
        one,
        busy=False,
        transitions=(Transition("queue_completed", one, observed_at=1.0, busy_epoch=1),),
    )
    for epoch in range(20):
        dispatcher.dispatch(reduction(one, busy=True), {})
        current = (
            first_complete
            if epoch == 0
            else reduction(
                one,
                busy=False,
                transitions=(
                    Transition(
                        "queue_completed", one, observed_at=epoch + 1.0, busy_epoch=epoch + 1
                    ),
                ),
            )
        )
        assert dispatcher.dispatch(current, {}) == 1
    assert dispatcher.dispatch(first_complete, {}) == 0

    dispatcher.dispatch(
        reduction(
            two,
            busy=True,
            transitions=(Transition("instance_replaced", two, observed_at=30.0),),
        ),
        {},
    )
    assert (
        dispatcher.dispatch(
            reduction(
                two,
                busy=False,
                transitions=(Transition("queue_completed", two, observed_at=31.0, busy_epoch=1),),
            ),
            {},
        )
        == 1
    )
    # Even replaying an old replacement cannot move the accepted generation back.
    old_replacement = reduction(
        one,
        busy=True,
        transitions=(Transition("instance_replaced", one, observed_at=0.5),),
    )
    assert dispatcher.dispatch(old_replacement, {}) == 0
    assert dispatcher.dispatch(first_complete, {}) == 0
    assert dispatcher.shutdown(1)
    assert len(sender.messages) == 21


def test_dispatcher_prunes_dedupe_state_only_when_endpoint_is_removed_from_settings():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    dispatcher = CompletionDispatcher(RecordingSender(), AppSettings(), dedupe_size=1)
    dispatcher.dispatch(reduction(endpoint, busy=True), {})
    complete = reduction(
        endpoint, busy=False, transitions=(Transition("queue_completed", endpoint, busy_epoch=1),)
    )
    assert dispatcher.dispatch(complete, {}) == 1
    assert ("127.0.0.1", 8188) in dispatcher._endpoint_progress

    other = replace(AppSettings(), endpoints=(EndpointConfig("127.0.0.2", 8189, "Other"),))
    dispatcher.update_settings(other)
    assert ("127.0.0.1", 8188) not in dispatcher._endpoint_progress
    dispatcher.update_settings(AppSettings())
    dispatcher.dispatch(reduction(endpoint, busy=True), {})
    assert dispatcher.dispatch(complete, {}) == 1
    assert dispatcher.shutdown(1)


def test_public_result_codes_cover_all_adapter_outcomes():
    assert RESULT_CODES == {
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


def test_dispatcher_queue_is_bounded_and_shutdown_timeout_is_bounded():
    endpoint = EndpointId("127.0.0.1", 8188, UUID(int=1))
    gate = threading.Event()
    dispatcher = CompletionDispatcher(RecordingSender(gate), AppSettings(), max_queue=1)
    dispatcher.dispatch(reduction(endpoint, busy=True), {})
    dispatcher.dispatch(
        reduction(
            endpoint,
            busy=False,
            transitions=(Transition("queue_completed", endpoint, busy_epoch=1),),
        ),
        {},
    )
    # Let the worker take the first item, then occupy the sole pending slot.
    time.sleep(0.02)
    dispatcher.dispatch(reduction(endpoint, busy=True), {})
    accepted = dispatcher.dispatch(
        reduction(
            endpoint,
            busy=False,
            transitions=(Transition("queue_completed", endpoint, busy_epoch=2),),
        ),
        {},
    )
    assert accepted in {0, 1}
    started = time.monotonic()
    assert dispatcher.shutdown(0.01) is False
    assert time.monotonic() - started < 0.2
    gate.set()
    assert dispatcher.shutdown(1)


@pytest.mark.parametrize("language", ["en-US", "zh-CN", "ja-JP", "ko-KR"])
def test_completion_text_is_localized_and_endpoint_bounded(language):
    text = completion_text(language, "x" * 500)
    assert len(text) < 300
    assert "x" * 256 in text
