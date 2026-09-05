import json
import subprocess
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
    assert argv[0] == sys.executable
    assert argv[-2:] == ["comfyui_progress_bridge.desktop.autostart", "127.0.0.1:8189"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    package_root = str(Path(desktop_launcher.__file__).resolve().parents[1])
    assert package_root in argv


def test_launcher_works_when_embedded_python_ignores_pythonpath(tmp_path, monkeypatch):
    from comfyui_progress_bridge import desktop_launcher

    root = tmp_path / "portable app '中文'"
    desktop = root / "comfyui_progress_bridge" / "desktop"
    desktop.mkdir(parents=True)
    (desktop.parent / "__init__.py").write_text(
        "import os\nassert os.environ['COMFY_PROGRESS_BRIDGE_COMPANION'] == '1'\n"
    )
    (desktop / "__init__.py").write_text("")
    (desktop / "autostart.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setattr(desktop_launcher, "__file__", str(desktop.parent / "desktop_launcher.py"))
    launches = []
    desktop_launcher.launch_desktop(
        8189, environ={}, popen=lambda argv, **kw: launches.append(argv)
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", *launches[0][1:]],
        cwd=tmp_path, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["127.0.0.1:8189"]


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


def test_companion_probe_does_not_import_or_parse_comfyui(tmp_path):
    from comfyui_progress_bridge.desktop_launcher import companion_argv

    # A checkout's cwd may contain ComfyUI; importing it in a child would parse
    # probe/desktop arguments as ComfyUI flags and may launch another server.
    (tmp_path / "comfy").mkdir()
    (tmp_path / "comfy" / "__init__.py").write_text("raise RuntimeError('imported host')")
    result = subprocess.run(
        companion_argv("comfyui_progress_bridge.monitor.remote_probe", "--help"),
        cwd=tmp_path, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "ComfyUI Progress Bridge]" not in result.stdout


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
