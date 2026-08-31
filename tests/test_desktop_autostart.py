import os
import sys
from pathlib import Path

from comfyui_progress_bridge.desktop.settings import AppSettings, EndpointConfig


def test_launcher_starts_detached_desktop_with_importing_port():
    from comfyui_progress_bridge import desktop_launcher

    launches = []

    class Process:
        pid = 4321

    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return Process()

    assert desktop_launcher.launch_desktop(8189, popen=popen, environ={}) is True
    assert len(launches) == 1
    argv, kwargs = launches[0]
    assert argv == [
        sys.executable,
        "-m",
        "comfyui_progress_bridge.desktop.autostart",
        "127.0.0.1:8189",
    ]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    package_root = str(Path(desktop_launcher.__file__).resolve().parents[1])
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == package_root


def test_launcher_can_be_disabled_for_headless_hosts():
    from comfyui_progress_bridge import desktop_launcher

    called = []
    assert (
        desktop_launcher.launch_desktop(
            8188,
            popen=lambda *args, **kwargs: called.append((args, kwargs)),
            environ={"COMFY_PROGRESS_BRIDGE_AUTOSTART": "0"},
        )
        is False
    )
    assert called == []


def test_launcher_failure_is_fail_open():
    from comfyui_progress_bridge import desktop_launcher

    def fail(*args, **kwargs):
        raise OSError("cannot spawn")

    assert desktop_launcher.launch_desktop(8188, popen=fail, environ={}) is False


def test_runtime_endpoint_override_uses_importing_comfy_port():
    from comfyui_progress_bridge.desktop import app as desktop_app

    persisted = AppSettings(
        endpoints=(EndpointConfig("127.0.0.1", 9000, "Persisted", "#123456"),)
    )
    runtime = desktop_app.runtime_settings(
        persisted,
        show=True,
        endpoint="127.0.0.1:8189",
    )
    assert runtime.dock_enabled is True
    assert runtime.endpoints == (
        EndpointConfig("127.0.0.1", 8189, "ComfyUI 8189", "#6C8EFF"),
    )
    assert persisted.endpoints[0].port == 9000


def test_autostart_singleton_exits_without_starting_second_ui():
    from comfyui_progress_bridge.desktop import autostart

    called = []
    assert (
        autostart.run(
            "127.0.0.1:8188",
            acquire_lock=lambda endpoint: None,
            app_main=lambda argv: called.append(argv) or 0,
        )
        == 0
    )
    assert called == []


def test_autostart_holds_lock_while_desktop_event_loop_runs():
    from comfyui_progress_bridge.desktop import autostart

    lock = object()
    called = []
    assert (
        autostart.run(
            "127.0.0.1:8188",
            acquire_lock=lambda endpoint: lock,
            app_main=lambda argv: called.append(argv) or 7,
        )
        == 7
    )
    assert called == [["--show", "--endpoint", "127.0.0.1:8188"]]


def test_singleton_lock_is_per_endpoint_and_released_on_close(tmp_path):
    from comfyui_progress_bridge.desktop.autostart import acquire_singleton_lock

    first = acquire_singleton_lock("127.0.0.1:8188", runtime_dir=tmp_path)
    assert first is not None
    assert acquire_singleton_lock("127.0.0.1:8188", runtime_dir=tmp_path) is None

    other = acquire_singleton_lock("127.0.0.1:8189", runtime_dir=tmp_path)
    assert other is not None
    other.close()
    first.close()

    replacement = acquire_singleton_lock("127.0.0.1:8188", runtime_dir=tmp_path)
    assert replacement is not None
    replacement.close()


def test_singleton_lock_uses_private_permissions(tmp_path):
    from comfyui_progress_bridge.desktop.autostart import acquire_singleton_lock

    runtime_dir = tmp_path / "private-runtime"
    lock = acquire_singleton_lock("127.0.0.1:8188", runtime_dir=runtime_dir)
    assert lock is not None
    lock_path = next(runtime_dir.glob("desktop-*.lock"))
    assert runtime_dir.stat().st_mode & 0o777 == 0o700
    assert lock_path.stat().st_mode & 0o777 == 0o600
    lock.close()
