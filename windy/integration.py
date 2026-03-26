from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .errors import WorkflowError

INIT_BLOCK_START = "-- BEGIN WINDY_RUNTIME_BLOCK"
INIT_BLOCK_END = "-- END WINDY_RUNTIME_BLOCK"


def install_hammerspoon(
    *,
    runtime_root: Path,
    executable_path: str,
    hs_bin: str,
) -> None:
    module_path = runtime_root / "hammerspoon" / "windy.lua"
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
            f'local ok, windy = pcall(dofile, {_lua_string(str(module_path))})',
            "if not ok then",
            '  print("windy load failed: " .. tostring(windy))',
            "else",
            f"  windy.start({{ windy_path = {_lua_string(executable_path)} }})",
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
    if INIT_BLOCK_START not in text or INIT_BLOCK_END not in text:
        return text
    start_index = text.index(INIT_BLOCK_START)
    end_index = text.index(INIT_BLOCK_END) + len(INIT_BLOCK_END)
    prefix = text[:start_index].rstrip()
    suffix = text[end_index:].lstrip()
    return prefix + ("\n\n" if prefix and suffix else "") + suffix


def _lua_string(value: str) -> str:
    return json.dumps(value)


def _is_expected_hammerspoon_reload_transport_error(detail: str) -> bool:
    lowered = detail.lower()
    return "message port was invalidated" in lowered
