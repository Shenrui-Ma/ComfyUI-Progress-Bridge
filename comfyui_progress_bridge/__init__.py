"""ComfyUI entry point for the external progress event bridge."""

from __future__ import annotations

from .bridge import install_bridge, resolve_target

NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}


def install_comfyui_bridge() -> bool:
    """Install the event mirror when running inside ComfyUI."""
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
            print(f"[ComfyUI Progress Bridge] UDP {host}:{port}")
        return installed
    except Exception as exc:
        # A monitoring extension must not prevent ComfyUI from starting.
        print(f"[ComfyUI Progress Bridge] disabled: {exc}")
        return False


install_comfyui_bridge()
