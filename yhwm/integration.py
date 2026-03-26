from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .errors import WorkflowError
from .yabai import YabaiClient

INIT_BLOCK_START = "-- BEGIN YHWM_RUNTIME_BLOCK"
INIT_BLOCK_END = "-- END YHWM_RUNTIME_BLOCK"


def install_hammerspoon(
    *,
    runtime_root: Path,
    executable_path: str,
    hs_bin: str,
) -> None:
    module_path = runtime_root / "hammerspoon" / "yhwm.lua"
    if not module_path.exists():
        raise WorkflowError(f"Hammerspoon module is missing: {module_path}")

    hammerspoon_home = Path.home() / ".hammerspoon"
    init_path = hammerspoon_home / "init.lua"
    hammerspoon_home.mkdir(parents=True, exist_ok=True)

    existing = ""
    if init_path.exists():
        try:
            existing = init_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Hammerspoon init is not readable: {init_path}") from exc

    without_old_block = _strip_managed_block(existing)
    base_content = without_old_block.rstrip()
    if 'require("hs.ipc")' not in base_content and "require('hs.ipc')" not in base_content:
        if base_content:
            base_content = 'require("hs.ipc")\n\n' + base_content
        else:
            base_content = 'require("hs.ipc")'

    block = "\n".join(
        [
            INIT_BLOCK_START,
            f'local ok, yhwm = pcall(dofile, {_lua_string(str(module_path))})',
            "if not ok then",
            '  print("yhwm load failed: " .. tostring(yhwm))',
            "else",
            f"  yhwm.start({{ yhwm_path = {_lua_string(executable_path)} }})",
            "end",
            INIT_BLOCK_END,
        ]
    )
    final_text = (base_content + "\n\n" + block).strip() + "\n"
    try:
        init_path.write_text(final_text, encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"Failed to write Hammerspoon init: {init_path}") from exc

    try:
        completed = subprocess.run(
            [hs_bin, "-c", "hs.reload()"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(f"Failed to invoke Hammerspoon CLI at '{hs_bin}'.") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        if _is_expected_hammerspoon_reload_transport_error(detail):
            return
        raise WorkflowError(f"Failed to reload Hammerspoon: {detail}")


def _strip_managed_block(text: str) -> str:
    block_markers = [
        (INIT_BLOCK_START, INIT_BLOCK_END),
        ("-- BEGIN YHWM_RUNTIME_V2", "-- END YHWM_RUNTIME_V2"),
        ("-- BEGIN YHWM_RUNTIME", "-- END YHWM_RUNTIME"),
    ]
    result = text
    for start_marker, end_marker in block_markers:
        if start_marker not in result or end_marker not in result:
            continue
        start_index = result.index(start_marker)
        end_index = result.index(end_marker) + len(end_marker)
        prefix = result[:start_index].rstrip()
        suffix = result[end_index:].lstrip()
        result = prefix + ("\n\n" if prefix and suffix else "") + suffix
    return result


def _lua_string(value: str) -> str:
    return json.dumps(value)


def _is_expected_hammerspoon_reload_transport_error(detail: str) -> bool:
    lowered = detail.lower()
    return "message port was invalidated" in lowered


def remove_legacy_yabai_signals(*, yabai: YabaiClient) -> None:
    for label in (
        "yhwm_v2_window_focused",
        "yhwm_v2_window_created",
        "yhwm_v2_window_deminimized",
        "yhwm_v2_window_moved",
        "yhwm_v2_window_minimized",
        "yhwm_v2_window_destroyed",
    ):
        try:
            yabai.remove_signal(label)
        except WorkflowError:
            pass
