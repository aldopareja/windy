from __future__ import annotations

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_window_record,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import BackgroundWindowReturnAsNewResult, WorkflowSpaceState
from .new_eligible_window import place_window_as_new_eligible
from .state import WorkflowStateStore
from .yabai import YabaiClient

WINDOW_DEMINIMIZED_EVENT = "window_deminimized"
SUPPORTED_BACKGROUND_WINDOW_RETURN_EVENTS = frozenset({WINDOW_DEMINIMIZED_EVENT})


class BackgroundWindowReturnAsNewService:
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

    def run(self) -> BackgroundWindowReturnAsNewResult:
        normalized_event = _normalize_supported_background_window_return_event(
            self._event
        )
        returning_window = query_window_record(
            self._yabai,
            window_id=self._window_id,
            description=f"returning window {self._window_id}",
        )
        workflow_space = derive_workflow_space_from_window(
            returning_window,
            description=f"returning window {self._window_id}",
        )
        if not is_eligible_window(
            returning_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return BackgroundWindowReturnAsNewResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=workflow_space,
                action="ignored_ineligible",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        persisted_space_state = self._state_store.read_space_state(workflow_space)
        if persisted_space_state is None:
            return BackgroundWindowReturnAsNewResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=workflow_space,
                action="ignored_untracked_space",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        if _is_window_tracked_anywhere(
            self._state_store.read_all_space_states(),
            window_id=self._window_id,
        ):
            return BackgroundWindowReturnAsNewResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=workflow_space,
                action="ignored_already_tracked",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
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
        placement = place_window_as_new_eligible(
            yabai=self._yabai,
            state_store=self._state_store,
            workflow_space=workflow_space,
            window_id=self._window_id,
            eligible_windows=eligible_windows,
            persisted_space_state=persisted_space_state,
            error_label="Returning eligible window",
        )
        return BackgroundWindowReturnAsNewResult(
            window_id=self._window_id,
            event=normalized_event,
            workflow_space=workflow_space,
            action=placement.action,
            visible_window_id=placement.visible_window_id,
            background_window_ids=placement.background_window_ids,
            pending_split_direction=None,
        )


def _is_window_tracked_anywhere(
    space_states: list[WorkflowSpaceState], *, window_id: int
) -> bool:
    return any(
        space_state.visible_window_id == window_id
        or window_id in space_state.background_window_ids
        for space_state in space_states
    )


def _normalize_supported_background_window_return_event(raw_event: str) -> str:
    candidate = raw_event.strip().lower()
    if candidate not in SUPPORTED_BACKGROUND_WINDOW_RETURN_EVENTS:
        raise WorkflowError(
            "background_window_return_as_new received an unsupported event: "
            f"{raw_event!r}"
        )
    return candidate
