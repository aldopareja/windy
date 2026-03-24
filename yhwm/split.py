from __future__ import annotations

from .current_space import (
    query_eligible_windows,
    require_focused_window_in_eligible_windows,
    resolve_current_space_target,
)
from .models import SplitResult
from .state import WorkflowStateStore
from .yabai import YabaiClient

DEFAULT_PENDING_SPLIT_DIRECTION = "east"


class SplitFromBackgroundPoolService:
    def __init__(self, yabai: YabaiClient, state_store: WorkflowStateStore):
        self._yabai = yabai
        self._state_store = state_store

    def run(self) -> SplitResult:
        target = resolve_current_space_target(
            self._yabai,
            allowed_layouts=("bsp",),
        )

        persisted_space_state = self._state_store.read_space_state(target.workflow_space)
        if persisted_space_state is None:
            persisted_background_window_ids: list[int] = []
        else:
            persisted_background_window_ids = list(
                persisted_space_state.background_window_ids
            )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        require_focused_window_in_eligible_windows(
            target.focused_window_id,
            eligible_windows,
        )

        current_eligible_window_ids = {window["id"] for window in eligible_windows}
        eligible_background_window_ids = [
            window_id
            for window_id in persisted_background_window_ids
            if window_id != target.focused_window_id and window_id in current_eligible_window_ids
        ]

        if not eligible_background_window_ids:
            prepared_state_payload = self._state_store.prepare_background_pool_payload(
                workflow_space=target.workflow_space,
                visible_window_id=target.focused_window_id,
                background_window_ids=[],
                pending_split_direction=DEFAULT_PENDING_SPLIT_DIRECTION,
            )
            self._yabai.arm_window_split(
                target.focused_window_id,
                DEFAULT_PENDING_SPLIT_DIRECTION,
            )
            self._state_store.write_payload(prepared_state_payload)
            return SplitResult(
                workflow_space=target.workflow_space,
                focused_window_id=target.focused_window_id,
                promoted_window_id=None,
                background_window_ids=[],
                pending_split_direction=DEFAULT_PENDING_SPLIT_DIRECTION,
            )

        promoted_window_id = eligible_background_window_ids[0]
        remaining_background_window_ids = [
            window_id
            for window_id in eligible_background_window_ids
            if window_id != promoted_window_id
        ]
        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=target.workflow_space,
            visible_window_id=target.focused_window_id,
            background_window_ids=remaining_background_window_ids,
            pending_split_direction=None,
        )

        # Background pool members are stacked behind the focused tile, so promotion
        # has to use yabai's supported unstack flow instead of same-stack warp.
        self._yabai.promote_stacked_window(
            promoted_window_id,
            DEFAULT_PENDING_SPLIT_DIRECTION,
        )
        self._yabai.focus_window(target.focused_window_id)
        self._state_store.write_payload(prepared_state_payload)

        return SplitResult(
            workflow_space=target.workflow_space,
            focused_window_id=target.focused_window_id,
            promoted_window_id=promoted_window_id,
            background_window_ids=remaining_background_window_ids,
            pending_split_direction=None,
        )
