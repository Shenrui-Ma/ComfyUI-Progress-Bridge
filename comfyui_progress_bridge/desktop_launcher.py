"""Fail-open launcher for the desktop companion from a ComfyUI import."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_AUTOSTART_FALSE = frozenset({"0", "false", "no", "off"})


def companion_argv(module: str, *arguments: str) -> list[str]:
    """Bootstrap our checkout even in Windows embedded Python with a ._pth file.

    Pass paths as argv data, never interpolate them into Python/shell code.
    Companion processes must not import ComfyUI's server or parse its CLI args.
    """
    bootstrap = (
        "import os,sys,runpy; "
        "os.environ['COMFY_PROGRESS_BRIDGE_COMPANION']='1'; "
        "sys.path.insert(0,sys.argv.pop(1)); "
        "runpy.run_module(sys.argv.pop(1),run_name='__main__',alter_sys=True)"
    )
    return [sys.executable, "-c", bootstrap, str(Path(__file__).resolve().parents[1]),
            module, *arguments]


def launch_desktop(
    comfy_port: int,
    *,
    environ: Mapping[str, str] | None = None,
    popen: Any = subprocess.Popen,
) -> bool:
    """Start one detached companion for this ComfyUI process.

    The child owns the cross-process singleton lock. Launch failures are deliberately
    contained so a desktop-only feature can never prevent ComfyUI from importing.
    """
    values = os.environ if environ is None else environ
    if (
        values.get("COMFY_PROGRESS_BRIDGE_AUTOSTART", "1").strip().casefold()
        in _AUTOSTART_FALSE
    ):
        return False
    if (
        isinstance(comfy_port, bool)
        or not isinstance(comfy_port, int)
        or not 1 <= comfy_port <= 65535
    ):
        return False

    argv = companion_argv("comfyui_progress_bridge.desktop.autostart", f"127.0.0.1:{comfy_port}")
    child_environ = dict(values)
    spawn_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": child_environ,
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI when available
        spawn_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        spawn_options["start_new_session"] = True
    try:
        popen(argv, **spawn_options)
    except Exception:
        return False
    return True
