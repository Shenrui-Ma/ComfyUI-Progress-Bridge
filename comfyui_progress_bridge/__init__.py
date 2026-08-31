"""ComfyUI entry point for the external progress event bridge."""

from __future__ import annotations

from collections.abc import Callable

from .bridge import install_bridge, resolve_target
from .desktop_launcher import launch_desktop

NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}


def install_comfyui_bridge(
    *, desktop_launcher: Callable[[int], bool] | None = None
) -> bool:
    """Install the event mirror and start its local desktop companion."""
    try:
        from comfy.cli_args import args
        from server import PromptServer
    except ModuleNotFoundError as exc:
        if exc.name in {"comfy", "comfy.cli_args", "server"}:
            return False
        raise

    try:
        comfy_port = int(args.port)
        installed = install_bridge(PromptServer, comfy_port)
        if installed:
            host, port = resolve_target(comfy_port)
            print(f"[ComfyUI Progress Bridge] schema 2 UDP {host}:{port}")
            try:
                requested = (desktop_launcher or launch_desktop)(comfy_port)
                status = "requested" if requested else "disabled or unavailable"
            except Exception as exc:
                status = f"unavailable: {exc}"
            print(f"[ComfyUI Progress Bridge] desktop launch {status}")
        return installed
    except Exception as exc:
        # A monitoring extension must not prevent ComfyUI from starting.
        print(f"[ComfyUI Progress Bridge] disabled: {exc}")
        return False


install_comfyui_bridge()
