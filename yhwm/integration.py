from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

from .errors import WorkflowError
from .yabai import YabaiClient

YABAI_SIGNAL_PREFIX = "yhwm_v2_"
INIT_BLOCK_START = "-- BEGIN YHWM_RUNTIME_V2"
INIT_BLOCK_END = "-- END YHWM_RUNTIME_V2"


def install_yabai_signals(*, yabai: YabaiClient, executable_path: str) -> None:
    for label in _signal_labels():
        try:
            yabai.remove_signal(label)
        except WorkflowError:
            pass

    quoted_executable = shlex.quote(executable_path)
    signal_specs = [
        (
            "window_focused",
            f'{quoted_executable} signal focus --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_focused",
        ),
        (
            "window_created",
            f'{quoted_executable} signal window --event window_created --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_created",
        ),
        (
            "window_deminimized",
            f'{quoted_executable} signal window --event window_deminimized --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_deminimized",
        ),
        (
            "window_moved",
            f'{quoted_executable} signal window --event window_moved --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_moved",
        ),
        (
            "window_minimized",
            f'{quoted_executable} signal window --event window_minimized --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_minimized",
        ),
        (
            "window_destroyed",
            f'{quoted_executable} signal window --event window_destroyed --window-id "$YABAI_WINDOW_ID"',
            f"{YABAI_SIGNAL_PREFIX}window_destroyed",
        ),
    ]

    for event, action, label in signal_specs:
        yabai.add_signal(event=event, action=action, label=label)


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


def _signal_labels() -> list[str]:
    return [
        f"{YABAI_SIGNAL_PREFIX}window_focused",
        f"{YABAI_SIGNAL_PREFIX}window_created",
        f"{YABAI_SIGNAL_PREFIX}window_deminimized",
        f"{YABAI_SIGNAL_PREFIX}window_moved",
        f"{YABAI_SIGNAL_PREFIX}window_minimized",
        f"{YABAI_SIGNAL_PREFIX}window_destroyed",
    ]


def _strip_managed_block(text: str) -> str:
    if INIT_BLOCK_START not in text or INIT_BLOCK_END not in text:
        return text
    start_index = text.index(INIT_BLOCK_START)
    end_index = text.index(INIT_BLOCK_END) + len(INIT_BLOCK_END)
    prefix = text[:start_index].rstrip()
    suffix = text[end_index:].lstrip()
    if prefix and suffix:
        return prefix + "\n\n" + suffix
    return prefix or suffix


def _lua_string(value: str) -> str:
    return json.dumps(value)


def _is_expected_hammerspoon_reload_transport_error(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "message port was invalidated" in lowered
        or "transport errors are normal if hammerspoon is reloading" in lowered
    )
