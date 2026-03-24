from __future__ import annotations

from .current_space import (
    derive_workflow_space_from_window,
    query_window_record,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import BackgroundWindowExitCleanupResult, WorkflowSpaceState
from .state import WorkflowStateStore
from .yabai import YabaiClient

WINDOW_DESTROYED_EVENT = "window_destroyed"
NON_DESTROYED_BACKGROUND_WINDOW_EXIT_EVENTS = frozenset(
    {"window_minimized", "window_moved"}
)
SUPPORTED_BACKGROUND_WINDOW_EXIT_EVENTS = (
    NON_DESTROYED_BACKGROUND_WINDOW_EXIT_EVENTS | frozenset({WINDOW_DESTROYED_EVENT})
)


class BackgroundWindowExitCleanupService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        window_id: int,
        event: str,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._window_id = window_id
        self._event = event

    def run(self) -> BackgroundWindowExitCleanupResult:
        matched_space_state = _resolve_matched_space_state(
            self._state_store.read_all_space_states(),
            window_id=self._window_id,
        )
        if matched_space_state is None:
            return BackgroundWindowExitCleanupResult(
                window_id=self._window_id,
                event=self._event,
                workflow_space=None,
                action="ignored_untracked_background_window",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=matched_space_state.workflow_space,
            allowed_layouts=("bsp",),
        )

        if self._event == WINDOW_DESTROYED_EVENT:
            return self._commit_background_window_removal(
                matched_space_state,
                action="removed_destroyed_background_window",
            )

        window_record = query_window_record(
            self._yabai,
            window_id=self._window_id,
            description=f"cleanup window {self._window_id}",
        )
        signaled_workflow_space = derive_workflow_space_from_window(
            window_record,
            description=f"cleanup window {self._window_id}",
        )
        if signaled_workflow_space == matched_space_state.workflow_space and is_eligible_window(
            window_record,
            target_display=matched_space_state.workflow_space.display,
            target_space=matched_space_state.workflow_space.space,
        ):
            return BackgroundWindowExitCleanupResult(
                window_id=self._window_id,
                event=self._event,
                workflow_space=matched_space_state.workflow_space,
                action="ignored_still_eligible",
                visible_window_id=matched_space_state.visible_window_id,
                background_window_ids=list(matched_space_state.background_window_ids),
                pending_split_direction=matched_space_state.pending_split_direction,
            )

        return self._commit_background_window_removal(
            matched_space_state,
            action="removed_ineligible_background_window",
        )

    def _commit_background_window_removal(
        self,
        space_state: WorkflowSpaceState,
        *,
        action: str,
    ) -> BackgroundWindowExitCleanupResult:
        refreshed_background_window_ids = [
            candidate_window_id
            for candidate_window_id in space_state.background_window_ids
            if candidate_window_id != self._window_id
        ]
        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=space_state.workflow_space,
            visible_window_id=space_state.visible_window_id,
            background_window_ids=refreshed_background_window_ids,
            pending_split_direction=space_state.pending_split_direction,
        )
        self._state_store.write_payload(prepared_state_payload)
        return BackgroundWindowExitCleanupResult(
            window_id=self._window_id,
            event=self._event,
            workflow_space=space_state.workflow_space,
            action=action,
            visible_window_id=space_state.visible_window_id,
            background_window_ids=refreshed_background_window_ids,
            pending_split_direction=space_state.pending_split_direction,
        )


def _resolve_matched_space_state(
    space_states: list[WorkflowSpaceState], *, window_id: int
) -> WorkflowSpaceState | None:
    matched_space_states = [
        space_state
        for space_state in space_states
        if window_id in space_state.background_window_ids
    ]
    if len(matched_space_states) > 1:
        raise WorkflowError(
            "Tracked background window appears in more than one tracked background "
            "window pool."
        )
    if not matched_space_states:
        return None
    return matched_space_states[0]
