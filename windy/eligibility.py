from __future__ import annotations

from typing import Any, Mapping


def is_eligible_window(
    window: Mapping[str, Any], *, target_display: int, target_space: int
) -> bool:
    """Return True when a yabai window record matches the workflow contract."""

    if window.get("display") != target_display:
        return False
    if window.get("space") != target_space:
        return False
    if not bool(window.get("root-window", False)):
        return False
    if window.get("role") != "AXWindow":
        return False
    if window.get("subrole") != "AXStandardWindow":
        return False
    if not bool(window.get("can-move", False)):
        return False
    if window.get("has-ax-reference") is False:
        return False
    if not _has_supported_window_level(window):
        return False
    if bool(window.get("is-floating", False)):
        return False
    if bool(window.get("is-sticky", False)):
        return False
    if bool(window.get("is-native-fullscreen", False)):
        return False
    if bool(window.get("is-minimized", False)):
        return False
    if bool(window.get("is-hidden", False)):
        return False
    return True


def _has_supported_window_level(window: Mapping[str, Any]) -> bool:
    """Accept window levels observed for supported managed workflow windows.

    yabai-managed windows can be reported at the normal layer, and on systems with
    the scripting addition available they may be moved into the below layer after
    tiling. Windows reported above the normal layer, or with an unknown layer, are
    not eligible workflow candidates.
    """

    layer = window.get("layer")
    if layer not in {"normal", "below"}:
        return False

    level = window.get("level")
    if level is None:
        return True
    if not isinstance(level, int):
        return False

    return level <= 0
