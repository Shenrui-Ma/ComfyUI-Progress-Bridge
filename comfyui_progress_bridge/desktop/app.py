"""Application entry point, live reducer adapter, and deterministic demo."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from comfyui_progress_bridge.monitor.models import (
    EndpointId,
    EndpointState,
    EventEnvelope,
    MonitorState,
    QueueSnapshot,
    Reduction,
    TaskKey,
    TaskState,
    Transition,
)
from comfyui_progress_bridge.monitor.reducer import MonitorReducer
from comfyui_progress_bridge.monitor.source import LocalSource, SSHSource, redact_error

from .audio import CompletionAudio
from .notifications import CompletionDispatcher, NotificationSender
from .settings import AppSettings, EndpointConfig, SettingsStore
from .widgets import ProgressWindow


def demo_settings(base: AppSettings | None = None) -> AppSettings:
    """Return stable multi-port settings suitable for screenshots and inspection."""
    source = base or AppSettings()
    return replace(
        source,
        endpoints=(
            EndpointConfig("127.0.0.1", 8188, "Local GPU", "#6C8EFF"),
            EndpointConfig("127.0.0.2", 8189, "Remote GPU", "#FF8A65"),
            EndpointConfig("127.0.0.2", 8190, "Queue worker", "#66BB6A"),
        ),
    )


def model_from_record(record: dict[str, Any]) -> QueueSnapshot | EventEnvelope | None:
    """Convert one strictly source-validated protocol record to reducer input."""
    if record.get("kind") not in {"snapshot", "event"}:
        return None
    endpoint_data = record["endpoint"]
    endpoint = EndpointId(endpoint_data["host"], endpoint_data["port"], UUID(record["instance_id"]))
    if record["kind"] == "snapshot":
        return QueueSnapshot(
            endpoint,
            record["online"],
            tuple(record["running_prompt_ids"]),
            tuple(record["pending_prompt_ids"]),
            record["observed_at"],
        )
    return EventEnvelope(
        endpoint,
        record["sequence"],
        record["observed_at"],
        record["type"],
        record["data"],
    )


class DesktopMonitor(QObject):
    """Thread-safe source-to-reducer adapter owned by the Qt event loop."""

    record_received = pyqtSignal(object)
    error_received = pyqtSignal(object)

    def __init__(
        self,
        window: ProgressWindow,
        settings: AppSettings,
        *,
        dispatcher: Any | None = None,
    ) -> None:
        super().__init__(window)
        self.window = window
        self.settings = settings
        self.reducer = MonitorReducer(terminal_retention=30.0)
        self.sources: list[LocalSource] = []
        self.errors: list[str] = []
        self.dispatcher = dispatcher or CompletionDispatcher(
            NotificationSender(), settings, audio=CompletionAudio()
        )
        self._generation = 0
        self.record_received.connect(self._consume_queued_record)
        self.error_received.connect(self._display_error)
        self.window.settings_applied.connect(self.apply_settings)
        self.expiry_timer = QTimer(self)
        self.expiry_timer.timeout.connect(self.expire)
        self.expiry_timer.start(1000)

    def consume_record(self, record: object) -> None:
        if not isinstance(record, dict):
            return
        model = model_from_record(record)
        if isinstance(model, QueueSnapshot):
            self._render(self.reducer.apply_snapshot(model))
        elif isinstance(model, EventEnvelope):
            self._render(self.reducer.apply_event(model))

    def _render(self, reduction: Reduction) -> None:
        self.window.render(reduction)
        names = {(item.host, item.port): item.name for item in self.settings.endpoints}
        self.dispatcher.dispatch(reduction, names)

    def _consume_queued_record(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, record = payload
        if generation == self._generation:
            self.consume_record(record)

    def expire(self) -> None:
        self._render(self.reducer.expire())

    def _error(self, message: str) -> None:
        self.error_received.emit((self._generation, redact_error(message)))

    def _display_error(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, message = payload
        if generation != self._generation:
            return
        clean = redact_error(message)
        self.errors.append(clean)
        del self.errors[:-20]
        logging.getLogger(__name__).error("probe source: %s", clean)
        self.window.show_source_error(clean)

    def _record_callback(self, generation: int, record: dict[str, Any]) -> None:
        self.record_received.emit((generation, record))

    def _error_callback(self, generation: int, message: str) -> None:
        self.error_received.emit((generation, redact_error(message)))

    def apply_settings(self, settings: AppSettings) -> None:
        """Stop old probes, reset reducer/UI state, then launch the new configuration."""
        if not isinstance(settings, AppSettings):
            return
        self.stop()
        self.settings = settings
        self.dispatcher.update_settings(settings)
        self.reducer = MonitorReducer(terminal_retention=30.0)
        self.window.render(Reduction(MonitorState()))
        self.start()

    def start(self) -> None:
        """Start one probe per complete transport, grouping all its endpoint ports."""
        if not self.settings.dock_enabled:
            return
        self._generation += 1
        generation = self._generation
        local_endpoints: list[EndpointConfig] = []
        ssh_groups: dict[tuple[str, int, str, str, str, str], list[EndpointConfig]] = {}
        for endpoint in self.settings.endpoints:
            if not endpoint.ssh_enabled:
                local_endpoints.append(endpoint)
                continue
            key = (
                endpoint.ssh_host.casefold(),
                endpoint.ssh_port,
                endpoint.ssh_user,
                endpoint.ssh_identity_file,
                endpoint.ssh_remote_python,
                endpoint.ssh_probe_path,
            )
            ssh_groups.setdefault(key, []).append(endpoint)

        if local_endpoints:
            targets = [f"{endpoint.host}:{endpoint.port}" for endpoint in local_endpoints]
            self.sources.append(
                LocalSource(
                    [
                        sys.executable,
                        "-m",
                        "comfyui_progress_bridge.monitor.remote_probe",
                        *targets,
                    ],
                    on_record=lambda record, token=generation: self._record_callback(token, record),
                    on_error=lambda message, token=generation: self._error_callback(token, message),
                )
            )

        for endpoints in ssh_groups.values():
            first = endpoints[0]
            probe = (
                [first.ssh_probe_path]
                if first.ssh_probe_path
                else ["-m", "comfyui_progress_bridge.monitor.remote_probe"]
            )
            targets = [f"{endpoint.host}:{endpoint.port}" for endpoint in endpoints]
            self.sources.append(
                SSHSource(
                    host=first.ssh_host,
                    port=first.ssh_port,
                    user=first.ssh_user,
                    remote_argv=[first.ssh_remote_python, *probe, *targets],
                    identity_file=first.ssh_identity_file or None,
                    on_record=lambda record, token=generation: self._record_callback(token, record),
                    on_error=lambda message, token=generation: self._error_callback(token, message),
                )
            )

        for source in self.sources:
            source.start()

    def stop(self) -> None:
        self._generation += 1
        old_sources, self.sources = self.sources, []
        for source in old_sources:
            source.stop()
        for source in old_sources:
            join = getattr(source, "join", None)
            if callable(join):
                join(timeout=2)

    def shutdown(self) -> None:
        """Stop probes and the bounded notification worker for application exit."""
        self.stop()
        self.dispatcher.shutdown()


def demo_reduction(step: int = 0) -> Reduction:
    """Create deterministic reducer output; every sixth frame completes one task."""
    configs = demo_settings().endpoints
    endpoints: dict[EndpointId, EndpointState] = {}
    tasks: dict[TaskKey, TaskState] = {}
    for index, config in enumerate(configs):
        endpoint = EndpointId(config.host, config.port, UUID(int=index + 1))
        endpoints[endpoint] = EndpointState(endpoint, online=index != 2, busy=index != 2)
        epoch = max(0, (step - 1) // 6) if index == 0 else 0
        key = TaskKey(endpoint, f"demo-{index}-{epoch}")
        value = min(20, (step * 2 + index * 3) % 21)
        tasks[key] = TaskState(
            key,
            "running" if index != 2 else "pending",
            "sampling" if index == 0 else "executing",
            "KSampler (synthetic demo)" if index == 0 else "Load checkpoint",
            "KSampler",
            value,
            20,
        )
    transitions = ()
    if step > 0 and step % 6 == 0:
        first_key = next(iter(tasks))
        first = tasks[first_key]
        tasks[first_key] = replace(
            first,
            status="success",
            progress_value=20,
            progress_max=20,
            terminal_at=float(step),
        )
        transitions = (Transition("task_success", first_key.endpoint, first_key),)
    return Reduction(MonitorState.from_parts(endpoints, tasks, {}), transitions)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native ComfyUI progress dock")
    parser.add_argument("--config", type=Path, help="override the settings JSON path")
    parser.add_argument("--demo", action="store_true", help="show deterministic synthetic activity")
    parser.add_argument(
        "--show",
        action="store_true",
        help="show the dock for this launch even when it is disabled in settings",
    )
    return parser


def runtime_settings(
    settings: AppSettings, *, demo: bool = False, show: bool = False
) -> AppSettings:
    """Apply process-local CLI overrides without persisting them."""
    result = demo_settings(settings) if demo else settings
    return replace(result, dock_enabled=True) if show else result


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("ComfyUI Progress Bridge")
    store = SettingsStore(arguments.config)
    persisted_settings = store.load()
    settings = runtime_settings(persisted_settings, demo=arguments.demo, show=arguments.show)
    window = ProgressWindow(settings, persisted_settings=persisted_settings, store=store)
    window.render(demo_reduction(0) if arguments.demo else Reduction(MonitorState()))
    if settings.dock_enabled:
        window.show()

    timer = None
    monitor = None
    if arguments.demo:
        timer = QTimer(window)
        counter = {"value": 0}

        def update_demo() -> None:
            counter["value"] += 1
            window.render(demo_reduction(counter["value"]))

        timer.timeout.connect(update_demo)
        timer.start(1000)
    else:
        monitor = DesktopMonitor(window, settings)
        application.aboutToQuit.connect(monitor.shutdown)
        monitor.start()
    return application.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
