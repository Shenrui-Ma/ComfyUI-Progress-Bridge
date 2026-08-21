"""Language-neutral stage classification for ComfyUI nodes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    key: str
    node_name: str | None


def stage_for_node(node_type: str | None, node_name: str | None) -> Stage:
    """Return a semantic localization key while preserving the raw node name."""
    normalized = (node_type or "").casefold()
    if "sampler" in normalized:
        key = "sampling"
    elif "checkpoint" in normalized or "loader" in normalized:
        key = "loading_model"
    elif "vae" in normalized and "decode" in normalized:
        key = "decoding"
    elif "save" in normalized:
        key = "saving"
    else:
        key = "executing"
    return Stage(key=key, node_name=node_name)
