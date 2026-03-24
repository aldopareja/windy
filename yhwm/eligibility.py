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

