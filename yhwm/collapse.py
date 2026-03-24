from __future__ import annotations

from .current_space import (
    query_eligible_windows,
    require_focused_window_in_eligible_windows,
    resolve_current_space_target,
)
from .models import CollapseResult
from .state import WorkflowStateStore
from .yabai import YabaiClient


class CollapseCurrentSpaceService:
    def __init__(self, yabai: YabaiClient, state_store: WorkflowStateStore):
        self._yabai = yabai
        self._state_store = state_store

    def run(self) -> CollapseResult:
        target = resolve_current_space_target(
            self._yabai,
            allowed_layouts=("bsp", "stack", "float"),
        )

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        require_focused_window_in_eligible_windows(
            target.focused_window_id,
            eligible_windows,
        )

        background_window_ids = [
            window["id"]
            for window in eligible_windows
            if window["id"] != target.focused_window_id
        ]

        result = CollapseResult(
            workflow_space=target.workflow_space,
            focused_window_id=target.focused_window_id,
            background_window_ids=background_window_ids,
        )
        prepared_state_payload = self._state_store.prepare_collapse_payload(result)

        self._yabai.set_space_layout(target.workflow_space.space, "bsp")
        for background_window_id in background_window_ids:
            self._yabai.stack_window(target.focused_window_id, background_window_id)
        self._yabai.focus_window(target.focused_window_id)
        self._state_store.write_payload(prepared_state_payload)
        return result
