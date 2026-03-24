from __future__ import annotations

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_recent_window_record,
    query_window_record,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import WindowFocusedResult
from .state import AltTabSessionStore, WorkflowStateStore
from .yabai import YabaiClient


class WindowFocusedService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        alttab_session_store: AltTabSessionStore | None = None,
        window_id: int,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._alttab_session_store = alttab_session_store
        self._window_id = window_id

    def run(self) -> WindowFocusedResult:
        if self._alttab_session_store is not None:
            armed_session = self._alttab_session_store.read_session()
            if armed_session is not None:
                return WindowFocusedResult(
                    focused_window_id=self._window_id,
                    workflow_space=armed_session.origin_workflow_space,
                    action="ignored_armed_alttab_session",
                    visible_window_id=None,
                    background_window_ids=[],
                    pending_split_direction=None,
                )
            focus_guard = self._alttab_session_store.read_focus_guard()
            if focus_guard is not None:
                focused_window = query_window_record(
                    self._yabai,
                    window_id=self._window_id,
                    description=f"focused window {self._window_id}",
                )
                workflow_space = derive_workflow_space_from_window(
                    focused_window,
                    description=f"focused window {self._window_id}",
                )
                self._alttab_session_store.clear_focus_guard()
                if workflow_space == focus_guard.workflow_space and (
                    focus_guard.target_window_id is None
                    or focus_guard.target_window_id == self._window_id
                ):
                    return WindowFocusedResult(
                        focused_window_id=self._window_id,
                        workflow_space=workflow_space,
                        action="ignored_recent_alttab_cancel",
                        visible_window_id=None,
                        background_window_ids=[],
                        pending_split_direction=None,
                    )

        focused_window = query_window_record(
            self._yabai,
            window_id=self._window_id,
            description=f"focused window {self._window_id}",
        )
        workflow_space = derive_workflow_space_from_window(
            focused_window,
            description=f"focused window {self._window_id}",
        )
        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return WindowFocusedResult(
                focused_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_ineligible",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        persisted_space_state = self._state_store.read_space_state(workflow_space)
        if persisted_space_state is None:
            return WindowFocusedResult(
                focused_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_untracked_space",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp",),
        )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        eligible_window_ids = {window["id"] for window in eligible_windows}
        if self._window_id not in eligible_window_ids:
            raise WorkflowError(
                "Focused eligible window is missing from the current eligible "
                "workflow window query for its workflow space."
            )

        if self._window_id in persisted_space_state.background_window_ids:
            return WindowFocusedResult(
                focused_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_background_window",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
            )

        previous_focused_window = query_recent_window_record(
            self._yabai,
            description="most recently focused window",
        )
        previous_focused_window_id = int(previous_focused_window["id"])
        if previous_focused_window_id == self._window_id:
            raise WorkflowError(
                "Failed to identify a previously focused window before the current "
                "focus change."
            )
        previous_workflow_space = derive_workflow_space_from_window(
            previous_focused_window,
            description="most recently focused window",
        )
        if previous_workflow_space != workflow_space:
            return WindowFocusedResult(
                focused_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_cross_space_or_display_transition",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
            )

        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=workflow_space,
            visible_window_id=self._window_id,
            background_window_ids=persisted_space_state.background_window_ids,
            pending_split_direction=persisted_space_state.pending_split_direction,
        )
        self._state_store.write_payload(prepared_state_payload)
        return WindowFocusedResult(
            focused_window_id=self._window_id,
            workflow_space=workflow_space,
            action="updated_focused_visible_tile",
            visible_window_id=self._window_id,
            background_window_ids=list(persisted_space_state.background_window_ids),
            pending_split_direction=persisted_space_state.pending_split_direction,
        )
