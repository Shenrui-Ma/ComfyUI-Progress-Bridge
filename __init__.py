"""Repository-root shim loaded by ComfyUI from custom_nodes."""

from __future__ import annotations

try:
    from . import comfyui_progress_bridge as _bridge_package
except ImportError:  # Supports direct execution in tests and diagnostics.
    import comfyui_progress_bridge as _bridge_package

NODE_CLASS_MAPPINGS = _bridge_package.NODE_CLASS_MAPPINGS
NODE_DISPLAY_NAME_MAPPINGS = _bridge_package.NODE_DISPLAY_NAME_MAPPINGS
WEB_DIRECTORY = "./comfyui_progress_bridge/web"
_bridge_package.install_comfyui_bridge()
