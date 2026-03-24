from __future__ import annotations

import json
import subprocess
from typing import Any, List, Optional, Protocol

from .errors import WorkflowError


class YabaiClient(Protocol):
    def get_config(self, setting: str, *, space: Optional[int] = None) -> str:
        ...

    def query_focused_window(self) -> Any:
        ...

    def query_display(self, display: int) -> Any:
        ...

    def query_space(self, space: int) -> Any:
        ...

    def query_windows_for_space(self, space: int) -> List[Any]:
        ...

    def set_space_layout(self, space: int, layout: str) -> None:
        ...

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        ...

    def focus_window(self, window_id: int) -> None:
        ...


class SubprocessYabaiClient:
    def __init__(self, yabai_bin: str = "yabai"):
        self._yabai_bin = yabai_bin

    def get_config(self, setting: str, *, space: Optional[int] = None) -> str:
        arguments = ["-m", "config"]
        if space is not None:
            arguments.extend(["--space", str(space)])
        arguments.append(setting)
        return self._run_text(
            arguments,
            error_context=f"Failed to read yabai config '{setting}'",
        )

    def query_focused_window(self) -> Any:
        payload = self._run_json(
            ["-m", "query", "--windows", "--window"],
            error_context="Failed to query the focused window from yabai",
        )
        return _expect_single_entity(payload, "focused window")

    def query_display(self, display: int) -> Any:
        payload = self._run_json(
            ["-m", "query", "--displays", "--display", str(display)],
            error_context=f"Failed to query display {display} from yabai",
        )
        return _expect_single_entity(payload, f"display {display}")

    def query_space(self, space: int) -> Any:
        payload = self._run_json(
            ["-m", "query", "--spaces", "--space", str(space)],
            error_context=f"Failed to query space {space} from yabai",
        )
        return _expect_single_entity(payload, f"space {space}")

    def query_windows_for_space(self, space: int) -> List[Any]:
        payload = self._run_json(
            ["-m", "query", "--windows", "--space", str(space)],
            error_context=f"Failed to query windows for space {space} from yabai",
        )
        if not isinstance(payload, list):
            raise WorkflowError(
                f"Expected yabai to return a window list for space {space}."
            )
        return payload

    def set_space_layout(self, space: int, layout: str) -> None:
        self._run_text(
            ["-m", "space", str(space), "--layout", layout],
            error_context=f"Failed to switch target space {space} to {layout}",
        )

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        self._run_text(
            ["-m", "window", str(anchor_window_id), "--stack", str(candidate_window_id)],
            error_context=(
                "Failed to move eligible window "
                f"{candidate_window_id} into the background pool"
            ),
        )

    def focus_window(self, window_id: int) -> None:
        self._run_text(
            ["-m", "window", "--focus", str(window_id)],
            error_context=f"Failed to refocus window {window_id} after collapse",
        )

    def _run_json(self, arguments: List[str], *, error_context: str) -> Any:
        output = self._run(arguments, error_context=error_context)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"{error_context}: invalid JSON output.") from exc

    def _run_text(self, arguments: List[str], *, error_context: str) -> str:
        return self._run(arguments, error_context=error_context).strip()

    def _run(self, arguments: List[str], *, error_context: str) -> str:
        command = [self._yabai_bin, *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Failed to invoke yabai at '{self._yabai_bin}'."
            ) from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise WorkflowError(f"{error_context}: {detail}")

        return completed.stdout


def _expect_single_entity(payload: Any, description: str) -> Any:
    if isinstance(payload, list):
        if len(payload) != 1:
            raise WorkflowError(f"Expected yabai to return exactly one {description}.")
        return payload[0]
    return payload
