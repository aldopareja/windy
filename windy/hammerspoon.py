from __future__ import annotations

import json
import subprocess
from typing import List, Protocol

from .errors import WorkflowError


class HammerspoonClient(Protocol):
    def ordered_window_ids(self) -> List[int]:
        ...


class SubprocessHammerspoonClient:
    def __init__(self, hs_bin: str = "hs"):
        self._hs_bin = hs_bin

    def ordered_window_ids(self) -> List[int]:
        script = (
            'local ids = {}; '
            'for _, win in ipairs(hs.window.orderedWindows()) do '
            '  local id = win:id(); '
            '  if id ~= nil then table.insert(ids, id); end; '
            'end; '
            'print(hs.json.encode(ids))'
        )
        try:
            completed = subprocess.run(
                [self._hs_bin, "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkflowError(f"Failed to invoke Hammerspoon CLI at '{self._hs_bin}'.") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise WorkflowError(f"Failed to query Hammerspoon ordered windows: {detail}")

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise WorkflowError("Failed to query Hammerspoon ordered windows: invalid JSON output.") from exc

        if not isinstance(payload, list) or any(not isinstance(item, int) for item in payload):
            raise WorkflowError(
                "Failed to query Hammerspoon ordered windows: expected a JSON array of integers."
            )
        return list(payload)
