import os
import threading
from dataclasses import replace
from datetime import datetime
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QKeyEvent
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from comfyui_progress_bridge.desktop import app as desktop_app
from comfyui_progress_bridge.desktop.app import DesktopMonitor, model_from_record
from comfyui_progress_bridge.desktop.i18n import Translator
from comfyui_progress_bridge.desktop.settings import AppSettings, EndpointConfig, SettingsStore
from comfyui_progress_bridge.desktop.widgets import ElidedLabel, ProgressWindow, SettingsDialog
from comfyui_progress_bridge.monitor.models import (
    EndpointId,
    EndpointState,
    MonitorState,
    Reduction,
    TaskKey,
    TaskState,
    Transition,
)


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def endpoints():
    return (
        EndpointConfig("127.0.0.1", 8188, "GPU one", "#6C8EFF"),
        EndpointConfig("127.0.0.1", 8189, "GPU two", "#FF8A65"),
        EndpointConfig("127.0.0.1", 8190, "GPU three", "#66BB6A"),
    )


def state_for(configs, *, node="KSampler with a very long descriptive title"):
    endpoint_states = {}
    tasks = {}
    for index, config in enumerate(configs):
        endpoint = EndpointId(config.host, config.port, UUID(int=index + 1))
        endpoint_states[endpoint] = EndpointState(endpoint, online=index != 2, busy=True)
        key = TaskKey(endpoint, f"p{index}")
        tasks[key] = TaskState(key, "running", "sampling", node, "KSampler", 7, 20)
    return MonitorState.from_parts(endpoint_states, tasks, {})


@pytest.mark.parametrize("language", ["zh-CN", "ja-JP", "en-US", "ko-KR"])
def test_settings_dialog_localizes_endpoint_headers_and_standard_buttons(app, language):
    dialog = SettingsDialog(AppSettings(language=language))
    translator = Translator(language)
    header_keys = (
        "name",
        "host",
        "port",
        "color",
        "ssh_source",
        "ssh_host",
        "ssh_user",
        "ssh_port",
        "identity_file",
        "remote_python",
        "probe_path",
    )
    headers = [
        dialog.endpoint_table.horizontalHeaderItem(column).text()
        for column in range(dialog.endpoint_table.columnCount())
    ]
    assert headers == [translator(key) for key in header_keys]

    button_box = dialog.findChild(QDialogButtonBox)
    assert button_box is not None
    assert button_box.button(QDialogButtonBox.StandardButton.Save).text() == translator("save")
    assert button_box.button(QDialogButtonBox.StandardButton.Cancel).text() == translator("cancel")

    if language != "en-US":
        english = Translator("en-US")
        localized_keys = header_keys[5:] + ("save", "cancel")
        assert all(translator(key) != english(key) for key in localized_keys)
    dialog.close()


def test_settings_dialog_is_resizable_scrollable_and_keeps_actions_visible(app):
    dialog = SettingsDialog(AppSettings(language="zh-CN"))
    dialog.show()
    app.processEvents()

    assert dialog.minimumWidth() < dialog.maximumWidth()
    assert dialog.minimumHeight() < dialog.maximumHeight()
    assert dialog.settings_scroll.widgetResizable()
    assert dialog.button_box.parentWidget() is dialog

    dialog.resize(540, 380)
    app.processEvents()
    save = dialog.button_box.button(QDialogButtonBox.StandardButton.Save)
    cancel = dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel)
    assert save.isVisible() and cancel.isVisible()
    assert dialog.button_box.geometry().bottom() <= dialog.contentsRect().bottom()
    assert dialog.settings_scroll.verticalScrollBar().maximum() > 0
    dialog.close()


def test_multiple_cards_have_stable_unique_nonoverlapping_layout(app, endpoints, tmp_path):
    window = ProgressWindow(AppSettings(endpoints=endpoints), store=SettingsStore(tmp_path / "x"))
    window.render(Reduction(state_for(endpoints)))
    window.show()
    app.processEvents()
    assert [card.config.port for card in window.cards] == [8188, 8189, 8190]
    rects = [card.geometry() for card in window.cards]
    assert all(rects[i].bottom() < rects[i + 1].top() for i in range(len(rects) - 1))
    assert window.scroll_area.maximumHeight() <= window.max_scroll_height
    window.close()


def test_idle_endpoint_cards_are_hidden_and_return_when_queue_runs(app, endpoints, tmp_path):
    window = ProgressWindow(
        AppSettings(endpoints=endpoints),
        store=SettingsStore(tmp_path / "hide-idle.json"),
    )
    window.show()
    app.processEvents()

    active_state = state_for(endpoints[:1])
    window.render(Reduction(active_state))
    app.processEvents()

    assert [not card.isHidden() for card in window.cards] == [True, False, False]
    assert not window.scroll_area.isHidden()
    assert window.scroll_area.height() == window.cards[0].height()

    window.render(Reduction(MonitorState()))
    app.processEvents()

    assert all(card.isHidden() for card in window.cards)
    assert window.scroll_area.isHidden()

    second_active = state_for(endpoints[1:2])
    window.render(Reduction(second_active))
    app.processEvents()

    assert [not card.isHidden() for card in window.cards] == [False, True, False]
    assert not window.scroll_area.isHidden()
    window.close()


def test_progress_dock_uses_three_quarter_scale_geometry_and_typography(
    app, endpoints, tmp_path
):
    window = ProgressWindow(
        AppSettings(mode="professional", endpoints=endpoints[:1]),
        store=SettingsStore(tmp_path / "three-quarter-scale.json"),
    )
    window.show()
    app.processEvents()

    assert window.width() == 264
    assert window.max_scroll_height == 353
    assert window.cards[0].width() == 246
    assert window.cards[0].height() == 113
    assert window.collapse_button.width() == 21
    assert window.gear_button.width() == 21
    assert window.close_button.width() == 21
    assert window.font().pixelSize() == 11
    text_widgets = (
        window.cards[0].endpoint_label,
        window.cards[0].queue_label,
        window.cards[0].stage_label,
        window.cards[0].node_label,
        window.cards[0].timestamp_label,
    )
    pixel_sizes = [widget.font().pixelSize() for widget in text_widgets]
    assert pixel_sizes == [11] * len(text_widgets)
    window.close()


def test_three_quarter_scale_professional_card_rows_do_not_overlap(app, endpoints, tmp_path):
    window = ProgressWindow(
        AppSettings(mode="professional", endpoints=endpoints[:1]),
        store=SettingsStore(tmp_path / "non-overlap.json"),
    )
    window.render(Reduction(state_for(endpoints[:1])))
    window.show()
    app.processEvents()
    card = window.cards[0]
    rows = [
        card.endpoint_label,
        card.queue_label,
        card.stage_label,
        card.node_label,
        card.progress_bar,
        card.timestamp_label,
    ]

    geometries = [
        (row.mapTo(card, QPoint(0, 0)).y(), row.height())
        for row in rows
    ]
    assert all(
        geometries[index][0] + geometries[index][1] <= geometries[index + 1][0]
        for index in range(len(rows) - 1)
    ), geometries
    assert geometries[-1][0] + geometries[-1][1] <= card.contentsRect().bottom() + 1
    window.close()


def test_simple_and_professional_modes(app, endpoints, tmp_path):
    simple = ProgressWindow(
        AppSettings(mode="simple", endpoints=endpoints[:1]), store=SettingsStore(tmp_path / "a")
    )
    simple.render(Reduction(state_for(endpoints[:1])))
    assert simple.cards[0].details.isHidden()
    assert simple.cards[0].status_label.isHidden()
    assert "1" in simple.cards[0].queue_label.text()
    pro = ProgressWindow(
        AppSettings(mode="professional", endpoints=endpoints[:1]),
        store=SettingsStore(tmp_path / "b"),
    )
    pro.render(Reduction(state_for(endpoints[:1])))
    assert not pro.cards[0].details.isHidden()
    text = " ".join(x.toolTip() for x in pro.cards[0].findChildren(ElidedLabel))
    assert "KSampler" in text
    assert "7 / 20" in pro.cards[0].progress_label.text()


def test_elision_retains_tooltip_and_does_not_change_bounds(app):
    label = ElidedLabel()
    label.setFixedWidth(90)
    full = "this is a deliberately extremely long node name"
    label.set_full_text(full)
    label.show()
    app.processEvents()
    assert label.toolTip() == full
    assert label.text() != full and "…" in label.text()
    assert label.width() == 90


def _png(path, color):
    image = QImage(24, 24, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")


def test_only_top_card_has_avatar_and_success_rotates_once(app, endpoints, tmp_path):
    paths = [tmp_path / f"{i}.png" for i in range(3)]
    for path, color in zip(paths, ["red", "green", "blue"], strict=True):
        _png(path, color)
    settings = AppSettings(
        endpoints=endpoints, avatar_enabled=True, avatar_paths=tuple(str(x) for x in paths)
    )
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings"))
    state = state_for(endpoints)
    window.render(Reduction(state))
    assert window.cards[0].avatar is not None
    assert window.avatar_index == 0
    assert all(card.avatar is None for card in window.cards[1:])
    first = window.avatar_index
    endpoint = next(iter(state.endpoints))
    task = next(iter(state.tasks))
    success = Reduction(state, (Transition("task_success", endpoint, task),))
    window.render(success)
    assert window.avatar_index == (first + 1) % 3
    window.render(success)
    assert window.avatar_index == (first + 1) % 3
    window.render(Reduction(state, (Transition("queue_completed", endpoint),)))
    assert window.avatar_index == (first + 1) % 3


def test_opacity_collapse_dock_and_reset_clamp(app, endpoints, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    window = ProgressWindow(AppSettings(opacity=61, endpoints=endpoints), store=store)
    window.render(Reduction(state_for(endpoints[:1])))
    window.show()
    app.processEvents()
    assert abs(window.windowOpacity() - 0.61) < 0.01
    window.set_collapsed(True)
    assert window.scroll_area.isHidden() and window.settings.collapsed
    window.set_collapsed(False)
    assert not window.scroll_area.isHidden()
    available = QRect(100, 200, 700, 500)
    point = window.reset_position(available)
    assert available.contains(point)
    window.move(99999, -99999)
    clamped = window.clamp_to(available)
    assert available.contains(clamped)
    window.set_dock_enabled(False)
    assert window.isHidden()
    assert SettingsStore(store.path).load().dock_enabled is False


def test_window_native_flags_and_controls(app, endpoints, tmp_path):
    from PyQt6.QtCore import Qt

    window = ProgressWindow(
        AppSettings(endpoints=endpoints[:1]), store=SettingsStore(tmp_path / "x")
    )
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert window.drag_handle.objectName() == "dragHandle"
    assert window.gear_button.toolTip()


def test_protocol_records_drive_reducer_and_thirty_second_expiry(app, endpoints, tmp_path):
    window = ProgressWindow(
        AppSettings(endpoints=endpoints[:1]), store=SettingsStore(tmp_path / "settings")
    )
    monitor = DesktopMonitor(window, window.settings)
    now = {"value": 10.0}
    monitor.reducer.clock = lambda: now["value"]
    common = {
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 10.0,
    }
    monitor.consume_record(
        {
            **common,
            "kind": "snapshot",
            "online": True,
            "running_prompt_ids": ["prompt"],
            "pending_prompt_ids": [],
        }
    )
    monitor.consume_record(
        {
            **common,
            "kind": "event",
            "sequence": 1,
            "type": "execution_success",
            "data": {"prompt_id": "prompt", "display_node": "Done"},
        }
    )
    assert window.cards[0].isHidden()
    assert len(monitor.reducer.state.tasks) == 1
    now["value"] = 39.999
    monitor.expire()
    assert len(monitor.reducer.state.tasks) == 1
    now["value"] = 40.0
    monitor.expire()
    assert not monitor.reducer.state.tasks
    assert model_from_record({"kind": "status"}) is None


def test_monitor_groups_probe_processes_by_transport(app, tmp_path, monkeypatch):
    created = []

    class FakeSource:
        def __init__(self, argv=None, **kwargs):
            self.argv = argv
            self.kwargs = kwargs
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            pass

    monkeypatch.setattr(desktop_app, "LocalSource", FakeSource)
    monkeypatch.setattr(desktop_app, "SSHSource", FakeSource)
    configs = (
        EndpointConfig("127.0.0.1", 8188, "Local one", "#111111"),
        EndpointConfig("127.0.0.1", 8189, "Local two", "#222222"),
        EndpointConfig(
            "127.0.0.1",
            8190,
            "Remote one",
            "#333333",
            ssh_enabled=True,
            ssh_host="worker-a",
            ssh_user="monitor",
        ),
        EndpointConfig(
            "127.0.0.1",
            8191,
            "Remote two",
            "#444444",
            ssh_enabled=True,
            ssh_host="worker-a",
            ssh_user="monitor",
        ),
        EndpointConfig(
            "127.0.0.1",
            8192,
            "Other worker",
            "#555555",
            ssh_enabled=True,
            ssh_host="worker-b",
            ssh_user="monitor",
        ),
    )
    settings = AppSettings(endpoints=configs)
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings"))
    monitor = DesktopMonitor(window, settings)
    monitor.start()

    local = [source for source in created if source.argv is not None]
    remote = [source for source in created if source.argv is None]
    assert len(local) == 1
    assert local[0].argv[-2:] == ["127.0.0.1:8188", "127.0.0.1:8189"]
    assert len(remote) == 2
    by_host = {source.kwargs["host"]: source for source in remote}
    assert by_host["worker-a"].kwargs["remote_argv"][-2:] == [
        "127.0.0.1:8190",
        "127.0.0.1:8191",
    ]
    assert by_host["worker-b"].kwargs["remote_argv"][-1:] == ["127.0.0.1:8192"]
    assert all(source.started for source in created)

    for offset, port in enumerate((8188, 8189), start=1):
        local[0].kwargs["on_record"](
            {
                "kind": "snapshot",
                "schema": 2,
                "endpoint": {"host": "127.0.0.1", "port": port},
                "instance_id": str(UUID(int=offset)),
                "observed_at": 1.0,
                "online": True,
                "running_prompt_ids": [f"prompt-{port}"],
                "pending_prompt_ids": [],
            }
        )
    assert {key.endpoint.port for key in monitor.reducer.state.tasks} == {8188, 8189}


def test_hidden_monitor_keeps_sources_and_queue_epoch_across_tray_toggle(
    app, tmp_path, monkeypatch
):
    created = []

    class FakeSource:
        def __init__(self, argv=None, **kwargs):
            self.argv = argv
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.joined = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.joined = timeout == 2

    monkeypatch.setattr(desktop_app, "LocalSource", FakeSource)
    settings = AppSettings(dock_enabled=False)
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, settings)
    monitor.start()
    monitor.start()
    assert len(created) == 1 and created[0].started
    monitor.consume_record({
        "kind": "snapshot",
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 1.0,
        "online": True,
        "running_prompt_ids": ["prompt"],
        "pending_prompt_ids": [],
    })
    busy_state = monitor.reducer.state

    window.show_action.trigger()
    assert window.isVisible()
    assert len(created) == 1 and created[0].started
    window.set_dock_enabled(False)
    assert window.isHidden()
    assert monitor.sources == created
    assert not created[0].stopped
    assert monitor.reducer.state is busy_state
    dispatched = []
    monkeypatch.setattr(
        monitor.dispatcher, "dispatch", lambda reduction, names: dispatched.append(reduction)
    )
    monitor.consume_record({
        "kind": "snapshot",
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 2.0,
        "online": True,
        "running_prompt_ids": [],
        "pending_prompt_ids": [],
    })
    assert [transition.kind for transition in dispatched[-1].transitions] == ["queue_completed"]
    monitor.shutdown()
    assert created[0].stopped and created[0].joined
    assert not monitor.expiry_timer.isActive()


def test_updated_time_tracks_accepted_source_records_not_repaints(app, tmp_path, monkeypatch):
    from comfyui_progress_bridge.desktop import widgets

    clock = {"value": datetime(2026, 9, 5, 12, 0, 0).astimezone()}

    class Clock:
        @staticmethod
        def now():
            return clock["value"]

    monkeypatch.setattr(widgets, "datetime", Clock)
    settings = AppSettings()
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "timestamp.json"))
    monitor = DesktopMonitor(window, settings)
    record = {
        "kind": "snapshot",
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 1.0,
        "online": True,
        "running_prompt_ids": ["prompt"],
        "pending_prompt_ids": [],
    }
    monitor.consume_record(record)
    assert window.cards[0].timestamp_label.text().endswith("12:00:00")
    clock["value"] = datetime(2026, 9, 5, 12, 5, 0).astimezone()
    monitor.expire()
    assert window.cards[0].timestamp_label.text().endswith("12:00:00")
    monitor.apply_settings(settings)
    assert window.cards[0].timestamp_label.text().endswith("12:00:00")
    monitor.consume_record({**record, "observed_at": 2.0})
    assert window.cards[0].timestamp_label.text().endswith("12:05:00")
    event = {
        "kind": "event",
        "endpoint": record["endpoint"],
        "instance_id": record["instance_id"],
        "observed_at": 3.0,
        "sequence": 1,
        "type": "progress",
        "data": {"prompt_id": "prompt", "value": 1, "max": 20},
    }
    monitor.consume_record(event)
    clock["value"] = datetime(2026, 9, 5, 12, 10, 0).astimezone()
    monitor.consume_record(event)
    assert window.cards[0].timestamp_label.text().endswith("12:05:00")
    monitor.shutdown()


def test_snapshot_workflow_metadata_enriches_events_and_expires_with_queue(app, tmp_path):
    window = ProgressWindow(AppSettings(), store=SettingsStore(tmp_path / "metadata.json"))
    monitor = DesktopMonitor(window, window.settings)
    common = {
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "observed_at": 1.0,
    }
    snapshot = {
        **common, "kind": "snapshot", "online": True,
        "running_prompt_ids": ["prompt"], "pending_prompt_ids": [],
        "workflows": {"prompt": {"12": {
            "display_node": "Portrait sampler", "node_type": "KSampler",
            "inputs": {"prompt": "private input"},
        }}},
    }
    monitor.consume_record(snapshot)
    event = {
        **common, "kind": "event", "sequence": 1, "type": "executing",
        "data": {"prompt_id": "prompt", "node": "12"},
    }
    monitor.consume_record(event)
    task = next(iter(monitor.reducer.state.tasks.values()))
    assert (task.node_name, task.node_type, task.stage_key) == (
        "Portrait sampler", "KSampler", "sampling"
    )
    assert "inputs" not in repr(monitor._workflow_metadata)
    assert event["data"] == {"prompt_id": "prompt", "node": "12"}
    snapshot["workflows"]["prompt"]["12"]["display_node"] = "mutated"
    monitor.consume_record({**event, "sequence": 2})
    assert next(iter(monitor.reducer.state.tasks.values())).node_name == "Portrait sampler"

    monitor.consume_record({**snapshot, "running_prompt_ids": [], "workflows": {}})
    assert monitor._workflow_metadata == {}
    monitor.consume_record({**snapshot, "workflows": {}})
    monitor.consume_record({**event, "sequence": 3})
    assert next(iter(monitor.reducer.state.tasks.values())).node_name == "12"
    monitor.shutdown()


def test_workflow_metadata_is_bounded_and_generation_scoped(app, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_app, "MAX_CACHED_WORKFLOW_NODES", 2)
    window = ProgressWindow(AppSettings(), store=SettingsStore(tmp_path / "bounded-metadata.json"))
    monitor = DesktopMonitor(window, window.settings)
    snapshot = {
        "kind": "snapshot", "online": True, "observed_at": 1.0,
        "endpoint": {"host": "127.0.0.1", "port": 8188},
        "instance_id": str(UUID(int=1)),
        "running_prompt_ids": ["prompt"], "pending_prompt_ids": [],
        "workflows": {"prompt": {
            str(index): {"display_node": "Sampler", "node_type": "KSampler"}
            for index in range(5)
        }},
    }
    monitor.consume_record(snapshot)
    assert sum(len(nodes) for nodes in monitor._workflow_metadata.values()) == 2
    # An offline snapshot from another process generation is rejected.
    monitor.consume_record({**snapshot, "instance_id": str(UUID(int=2)), "online": False})
    assert all(key.endpoint.instance_id == UUID(int=1) for key in monitor._workflow_metadata)
    monitor.consume_record({**snapshot, "instance_id": str(UUID(int=2)), "workflows": {}})
    assert monitor._workflow_metadata == {}
    monitor.shutdown()


def test_live_settings_restart_uses_complete_transport_key(app, tmp_path, monkeypatch):
    created = []

    class FakeSource:
        def __init__(self, argv=None, **kwargs):
            self.argv = argv
            self.kwargs = kwargs
            self.stopped = False
            created.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(desktop_app, "LocalSource", FakeSource)
    monkeypatch.setattr(desktop_app, "SSHSource", FakeSource)
    initial = AppSettings()
    window = ProgressWindow(initial, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, initial)
    monitor.start()
    old = created[0]
    changed = AppSettings(
        endpoints=(
            EndpointConfig(
                "127.0.0.1",
                8188,
                "worker one",
                "#111111",
                ssh_enabled=True,
                ssh_host="worker",
                ssh_user="alice",
            ),
            EndpointConfig(
                "127.0.0.1",
                8189,
                "worker two",
                "#222222",
                ssh_enabled=True,
                ssh_host="worker",
                ssh_user="bob",
            ),
        )
    )
    window.apply_settings(changed)
    window.settings_applied.emit(changed)
    assert old.stopped
    remote = [source for source in created[1:] if source.argv is None]
    assert len(remote) == 2
    assert {source.kwargs["user"] for source in remote} == {"alice", "bob"}
    assert monitor.reducer.state == MonitorState()


def test_language_only_settings_change_keeps_live_probe_and_monitor_state(
    app, tmp_path, monkeypatch
):
    created = []

    class FakeSource:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(desktop_app, "LocalSource", FakeSource)
    initial = AppSettings(
        language="zh-CN", endpoints=(EndpointConfig("127.0.0.1", 8191, "H3"),)
    )
    window = ProgressWindow(initial, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, initial)
    monitor.start()
    source = created[0]
    previous_reducer = monitor.reducer

    monitor.apply_settings(replace(initial, language="en-US"))

    assert source.started
    assert not source.stopped
    assert created == [source]
    assert monitor.reducer is previous_reducer


def test_source_errors_are_queued_redacted_bounded_and_visible(app, tmp_path):
    settings = AppSettings(language="zh-CN")
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, window.settings)
    thread = threading.Thread(target=monitor._error, args=("password=hunter2 connection refused",))
    thread.start()
    thread.join()
    app.processEvents()
    assert monitor.errors == ["password=[REDACTED] connection refused"]
    assert window.source_status.text() == "连接被拒绝，正在重连…"
    assert "hunter2" not in window.source_status.text()
    assert "connection refused" not in window.source_status.toolTip()
    assert not window.source_status.isHidden()
    assert all("hunter2" not in card.status_label.toolTip() for card in window.cards)


def test_source_error_is_rule_classified_without_raw_details(app, tmp_path):
    settings = AppSettings(language="zh-CN")
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, window.settings)

    monitor._display_error((monitor._generation, "Timeout, server 172.18.132.41 not responding"))
    assert window.source_status.text() == "连接超时，正在重连…"
    assert "172.18.132.41" not in window.source_status.text()
    assert "172.18.132.41" not in window.source_status.toolTip()

    monitor._display_error((monitor._generation, "unexpected probe failure"))
    assert window.source_status.text() == "监控连接异常，正在重连…"
    assert "unexpected probe failure" not in window.source_status.toolTip()


def test_valid_probe_record_clears_error_and_restores_window_height(app, tmp_path):
    settings = AppSettings(endpoints=(EndpointConfig("127.0.0.1", 8202, "H3"),))
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, settings)
    window.show()
    app.processEvents()
    normal_height = window.height()

    monitor._display_error(
        (
            monitor._generation,
            "Timeout, server 172.18.132.41 not responding with a deliberately long detail " * 4,
        )
    )
    app.processEvents()
    error_height = window.height()
    assert error_height >= normal_height

    monitor.consume_record(
        {
            "kind": "snapshot",
            "schema": 2,
            "endpoint": {"host": "127.0.0.1", "port": 8202},
            "instance_id": str(UUID(int=1)),
            "observed_at": 10.0,
            "online": True,
            "running_prompt_ids": [],
            "pending_prompt_ids": [],
        }
    )
    app.processEvents()

    assert window.source_status.isHidden()
    assert window.source_status.text() == ""
    assert window.height() == normal_height
    assert all("server not responding" not in card.status_label.toolTip() for card in window.cards)


def test_probe_recovery_clears_only_the_matching_source_error(app, tmp_path):
    settings = AppSettings(
        language="zh-CN",
        endpoints=(
            EndpointConfig("127.0.0.1", 8202, "Source A"),
            EndpointConfig("127.0.0.1", 8203, "Source B"),
        )
    )
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, settings)
    source_a, source_b = object(), object()

    def snapshot(port, instance):
        return {
            "kind": "snapshot",
            "schema": 2,
            "endpoint": {"host": "127.0.0.1", "port": port},
            "instance_id": str(UUID(int=instance)),
            "observed_at": 10.0,
            "online": True,
            "running_prompt_ids": [],
            "pending_prompt_ids": [],
        }

    monitor._display_error((monitor._generation, source_a, "connection timed out"))
    monitor.consume_record(snapshot(8203, 2), source_b)
    assert window.source_status.text() == Translator("zh-CN")("source_timeout")

    monitor._display_error((monitor._generation, source_b, "permission denied"))
    assert window.source_status.text() == Translator("zh-CN")("source_auth")
    monitor.consume_record(snapshot(8203, 2), source_b)
    assert window.source_status.text() == Translator("zh-CN")("source_timeout")

    monitor.consume_record(snapshot(8202, 1), source_a)
    assert window.source_status.isHidden()


def test_stale_event_does_not_clear_its_source_error(app, tmp_path):
    settings = AppSettings(
        language="zh-CN", endpoints=(EndpointConfig("127.0.0.1", 8202, "H3"),)
    )
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, settings)
    source = object()
    common = {
        "kind": "event",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8202},
        "instance_id": str(UUID(int=1)),
        "observed_at": 10.0,
        "type": "executing",
        "data": {"prompt_id": "prompt", "display_node": "KSampler"},
    }

    monitor.consume_record({**common, "sequence": 2}, source)
    monitor._display_error((monitor._generation, source, "connection timed out"))
    monitor.consume_record({**common, "sequence": 1}, source)
    assert window.source_status.text() == Translator("zh-CN")("source_timeout")

    monitor.consume_record({**common, "sequence": 3}, source)
    assert window.source_status.isHidden()


def test_rejected_absent_prompt_event_does_not_clear_source_error(app, tmp_path):
    settings = AppSettings(
        language="zh-CN", endpoints=(EndpointConfig("127.0.0.1", 8202, "H3"),)
    )
    window = ProgressWindow(settings, store=SettingsStore(tmp_path / "settings.json"))
    monitor = DesktopMonitor(window, settings)
    source = object()
    snapshot = {
        "kind": "snapshot",
        "schema": 2,
        "endpoint": {"host": "127.0.0.1", "port": 8202},
        "instance_id": str(UUID(int=1)),
        "observed_at": 10.0,
        "online": True,
        "running_prompt_ids": [],
        "pending_prompt_ids": [],
    }
    monitor.consume_record(snapshot, source)
    monitor._display_error((monitor._generation, source, "connection timed out"))

    monitor.consume_record(
        {
            "kind": "event",
            "schema": 2,
            "endpoint": {"host": "127.0.0.1", "port": 8202},
            "instance_id": str(UUID(int=1)),
            "observed_at": 11.0,
            "sequence": 1,
            "type": "progress",
            "data": {"prompt_id": "absent", "value": 1, "max": 20},
        },
        source,
    )
    assert window.source_status.text() == Translator("zh-CN")("source_timeout")

    monitor.consume_record({**snapshot, "running_prompt_ids": ["present"]}, source)
    assert window.source_status.isHidden()


def test_validation_feedback_accessibility_keyboard_and_reclamp(app, tmp_path, monkeypatch):
    from comfyui_progress_bridge.desktop import widgets

    warnings = []
    monkeypatch.setattr(widgets.QMessageBox, "warning", lambda *args: warnings.append(args))
    dialog = SettingsDialog(AppSettings())
    dialog.endpoint_table.item(0, 1).setText("localhost")
    dialog._validate_and_accept()
    assert "host" in dialog.validation_label.text()
    assert warnings and dialog.result() != dialog.DialogCode.Accepted

    window = ProgressWindow(AppSettings(), store=SettingsStore(tmp_path / "settings.json"))
    assert window.drag_handle.accessibleName()
    assert window.collapse_button.accessibleName()
    assert window.gear_button.accessibleName()
    assert window.close_button.accessibleDescription()
    before = window.pos()
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Right,
        Qt.KeyboardModifier.AltModifier,
    )
    window.drag_handle.keyPressEvent(event)
    assert window.x() >= before.x()
    window.move(99999, 99999)
    window.schedule_clamp()
    app.processEvents()
    screen = QApplication.primaryScreen()
    assert screen is not None and screen.availableGeometry().contains(window.pos())


def test_show_cli_flag_is_runtime_only(tmp_path):
    assert desktop_app._parser().parse_args(["--show"]).show is True
    assert desktop_app._parser().parse_args([]).show is False
    path = tmp_path / "settings.json"
    SettingsStore(path).save(AppSettings(dock_enabled=False))
    persisted = SettingsStore(path).load()
    shown = desktop_app.runtime_settings(persisted, show=True)
    assert shown.dock_enabled is True
    assert persisted.dock_enabled is False
    assert SettingsStore(path).load().dock_enabled is False


def test_show_override_does_not_leak_during_window_initialization(app, tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    persisted = AppSettings(dock_enabled=False)
    store.save(persisted)

    window = ProgressWindow(
        desktop_app.runtime_settings(persisted, show=True),
        persisted_settings=persisted,
        store=store,
    )

    assert window.settings.dock_enabled is True
    assert store.load().dock_enabled is False
    window.close()


def test_language_save_keeps_force_shown_dock_visible(app, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    persisted = AppSettings(language="zh-CN", dock_enabled=False)
    store.save(persisted)
    runtime = desktop_app.runtime_settings(persisted, show=True)
    dialog_bases = []

    class LanguageOnlyDialog:
        def __init__(self, settings, parent):
            self.settings = settings
            dialog_bases.append(settings)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_settings(self):
            return replace(self.settings, language="en-US")

    monkeypatch.setattr(
        "comfyui_progress_bridge.desktop.widgets.SettingsDialog", LanguageOnlyDialog
    )
    window = ProgressWindow(runtime, persisted_settings=persisted, store=store)
    window.show()
    app.processEvents()

    window.open_settings()
    app.processEvents()

    assert dialog_bases[0].dock_enabled is True
    assert window.isVisible()
    assert window.settings.language == "en-US"
    assert store.load().dock_enabled is True
    window.close()


def test_demo_endpoints_never_leak_from_internal_window_saves(app, tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    persisted = AppSettings(
        endpoints=(EndpointConfig("127.0.0.1", 9000, "Real endpoint", "#123456"),)
    )
    store.save(persisted)
    runtime = desktop_app.runtime_settings(persisted, demo=True)

    window = ProgressWindow(runtime, persisted_settings=persisted, store=store)
    assert store.load().endpoints == persisted.endpoints
    window.reset_position(QRect(0, 0, 800, 600))
    assert store.load().endpoints == persisted.endpoints
    window.move(50, 50)
    window.persist_position()
    assert store.load().endpoints == persisted.endpoints
    window.set_collapsed(True)
    assert store.load().endpoints == persisted.endpoints
    window.close()


def test_dialog_uses_persisted_base_with_effective_visibility_and_applies_deliberate_changes(
    app, tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    persisted = AppSettings(dock_enabled=False)
    store.save(persisted)
    runtime = desktop_app.runtime_settings(persisted, demo=True, show=True)
    deliberate = AppSettings(
        dock_enabled=False,
        endpoints=(EndpointConfig("127.0.0.1", 9100, "Chosen endpoint", "#ABCDEF"),),
    )
    seen = []

    class AcceptedDialog:
        def __init__(self, settings, parent):
            seen.append(settings)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_settings(self):
            return deliberate

    monkeypatch.setattr("comfyui_progress_bridge.desktop.widgets.SettingsDialog", AcceptedDialog)
    window = ProgressWindow(runtime, persisted_settings=persisted, store=store)
    initialized_base = store.load()
    window.open_settings()

    assert seen == [replace(initialized_base, dock_enabled=True)]
    assert store.load() == deliberate
    assert window.persisted_settings == deliberate
    assert window.settings == deliberate
    window.close()
